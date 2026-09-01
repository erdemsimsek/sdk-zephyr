# Copyright (c) 2026 Nordic Semiconductor ASA.
#
# SPDX-License-Identifier: Apache-2.0

'''Managed-MRAM nrfutil/PyLink runner for nRF7120 engineering PDKs.'''

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from runners.core import RunnerCaps, ZephyrBinaryRunner

try:
    from intelhex import IntelHex
except ImportError:
    IntelHex = None

try:
    import pylink
except ImportError:
    pylink = None


CTRL_AP = 2
CTRL_AP_RESET = 0x000
CTRL_AP_ERASEALL = 0x004
CTRL_AP_ERASEALLSTATUS = 0x008
CTRL_AP_BOOTSTATUS = 0x038

ERASEALL_READY = 0
ERASEALL_READY_TO_RESET = 1
ERASEALL_BUSY = 2
ERASEALL_ERROR = 3
CTRL_AP_NO_RESET = 0
CTRL_AP_HARD_RESET = 2
LCS_TEST = 0x18888

CM33_AP = 0
MEM_AP_CSW = 0x000
MEM_AP_TAR = 0x004
MEM_AP_DRW = 0x00C
MEM_AP_CSW_CONFIG_MASK = (
    (1 << 31) | (0x7F << 24) | (0xF << 8) | (0x3 << 4) | 0x7
)
MEM_AP_CSW_WORD_TRANSFER = (3 << 24) | (1 << 4) | 2

MRAMC_WEN = 0x5004E500
MRAMC_READY = 0x5004E400
MRAMC_READYNEXT = 0x5004E404
MRAMC_WAITSTATES = 0x5004E508
MRAMC_CONFIGNVR0 = 0x5004E580
MRAMC_READY_READY = 1
MRAMC_WEN_DISABLE = 0
MRAMC_WEN_DIRECT_WRITE = 2
MRAMC_CONFIGNVR_DISABLE = 0
MRAMC_CONFIGNVR_WRITE_ERASE = 22
MRAMC_WAITSTATES_KEY = 0xA66D

CPUCONF_CPUSTART = 0x50073508
CPUCONF_CPUWAIT = 0x5007350C
SCB_VTOR = 0xE000ED08
DHCSR = 0xE000EDF0
DCRSR = 0xE000EDF4
DCRDR = 0xE000EDF8
DEMCR = 0xE000EDFC

DHCSR_HALT = 0xA05F0003
DHCSR_RESUME = 0xA05F0001
DEMCR_VC_CORERESET = 0x00000001
DCRSR_WRITE = 0x00010000
REGSEL_PC = 0x0F
REGSEL_XPSR = 0x10
REGSEL_MSP = 0x11
XPSR_THUMB = 0x01000000
SRAM_BASE = 0x20000000
SRAM_MASK = 0xFFF00000
NRF7120_PARTNO = 0x2C
NRFUTIL_CORE_APPLICATION = 'NRFDL_DEVICE_CORE_APPLICATION'


class Nrf7120PdkBinaryRunner(ZephyrBinaryRunner):
    '''Erase/program with nrfutil, then start an nRF7120 PDK through PyLink.'''

    def __init__(self, cfg, dev_id=None, speed=1000, erase_timeout=10,
                 mram_waitstates=6, reset=True, dry_run=False, hex_files=None,
                 startup=None, nrfutil='nrfutil'):
        super().__init__(cfg)
        self.dev_id = dev_id
        self.speed = speed
        self.erase_timeout = erase_timeout
        self.mram_waitstates = mram_waitstates
        self.finish = bool(reset)
        self.dry_run = dry_run
        self.hex_files = list(hex_files or [])
        self.startup = startup
        self.nrfutil = nrfutil

    @classmethod
    def name(cls):
        return 'nrf7120-pdk'

    @classmethod
    def capabilities(cls):
        return RunnerCaps(commands={'flash'}, dev_id=True, reset=True,
                          dry_run=True)

    @classmethod
    def dev_id_help(cls):
        return ('J-Link serial number; if omitted, exactly one connected '
                'J-Link is selected automatically')

    @classmethod
    def do_add_parser(cls, parser):
        parser.add_argument('--speed', type=int, default=1000,
                            help='SWD speed in kHz (default: 1000)')
        parser.add_argument('--erase-timeout', type=float, default=10,
                            help='CTRL-AP ERASEALL timeout in seconds')
        parser.add_argument('--mram-waitstates',
                            type=lambda value: int(value, 0), default=6,
                            help='MRAMC.WAITSTATENUM before start (default: 6)')
        parser.add_argument('--nrfutil', default='nrfutil',
                            help='nrfutil executable (default: nrfutil)')
        parser.set_defaults(reset=True)

    @classmethod
    def do_create(cls, cfg, args):
        return cls(cfg, args.dev_id, speed=args.speed,
                   erase_timeout=args.erase_timeout,
                   mram_waitstates=args.mram_waitstates, reset=args.reset,
                   dry_run=args.dry_run,
                   hex_files=getattr(args, 'nrf7120_pdk_hex_files', None),
                   startup=getattr(args, 'nrf7120_pdk_startup', None),
                   nrfutil=args.nrfutil)

    @classmethod
    def args_from_previous_runner(cls, previous_runner, args):
        if args.dev_id is None:
            args.dev_id = previous_runner.dev_id
        args.nrf7120_pdk_hex_files = previous_runner.hex_files
        args.nrf7120_pdk_startup = previous_runner.startup

    @staticmethod
    def _word(image, address):
        data = image.tobinarray(start=address, size=4)
        return int.from_bytes(bytes(data), 'little')

    def _validate_and_collect(self):
        if not self.build_conf.getboolean('CONFIG_SOC_NRF7120_ENGA_CPUAPP'):
            raise RuntimeError(
                'the nrf7120-pdk runner requires an nRF7120 '
                'engineering-silicon CPUAPP build')

        hex_file = self.cfg.hex_file
        if not hex_file or not os.path.isfile(hex_file):
            raise RuntimeError(f'HEX file not found: {hex_file}')
        if hex_file not in self.hex_files:
            self.hex_files.append(hex_file)

        if self.startup is not None:
            return
        if IntelHex is None:
            raise RuntimeError('Python dependency intelhex is required')

        vtor = (self.build_conf.get('CONFIG_FLASH_BASE_ADDRESS', 0) +
                self.build_conf.get('CONFIG_FLASH_LOAD_OFFSET', 0))

        image = IntelHex()
        image.loadfile(hex_file, format='hex')
        sp = self._word(image, vtor)
        pc = self._word(image, vtor + 4)
        if (sp & SRAM_MASK) == SRAM_BASE and pc & 1:
            self.startup = (vtor, sp, pc & ~1)

    def _select_probe(self, jlink):
        if self.dev_id is not None:
            return int(self.dev_id)

        probes = jlink.connected_emulators()
        if not probes:
            raise RuntimeError('no connected J-Link probes found')
        if len(probes) != 1:
            serials = ', '.join(str(probe.SerialNumber) for probe in probes)
            raise RuntimeError(
                f'multiple J-Link probes found ({serials}); use --dev-id')
        self.dev_id = str(probes[0].SerialNumber)
        return probes[0].SerialNumber

    @staticmethod
    def _select_ap_bank(jlink, ap, address):
        select = (ap << 24) | (address & 0xF0)
        jlink.coresight_write(reg=2, data=select, ap=False)

    def _ap_read(self, jlink, ap, address):
        self._select_ap_bank(jlink, ap, address)
        return jlink.coresight_read(reg=(address & 0x0C) // 4, ap=True)

    def _ap_write(self, jlink, ap, address, value):
        self._select_ap_bank(jlink, ap, address)
        jlink.coresight_write(reg=(address & 0x0C) // 4,
                             data=value, ap=True)

    def _mem_ap_write32(self, jlink, address, value):
        csw = self._ap_read(jlink, CM33_AP, MEM_AP_CSW)
        csw &= ~MEM_AP_CSW_CONFIG_MASK
        csw |= MEM_AP_CSW_WORD_TRANSFER
        self._ap_write(jlink, CM33_AP, MEM_AP_CSW, csw)
        self._ap_write(jlink, CM33_AP, MEM_AP_TAR, address)
        self._ap_write(jlink, CM33_AP, MEM_AP_DRW, value)

    def _require_test_lcs(self, jlink):
        deadline = time.monotonic() + self.erase_timeout
        while True:
            bootstatus = self._ap_read(jlink, CTRL_AP, CTRL_AP_BOOTSTATUS)
            lcs = (bootstatus >> 12) & 0xFFFFF
            if lcs == LCS_TEST:
                return
            if lcs not in (0, 0x3DBEE) or time.monotonic() >= deadline:
                raise RuntimeError(
                    f'nRF7120 must already be in TEST LCS; '
                    f'CTRL-AP BOOTSTATUS={bootstatus:#010x}, LCS={lcs:#07x}')
            time.sleep(0.01)

    def _erase_all(self, jlink):
        self._ap_write(jlink, CTRL_AP, CTRL_AP_ERASEALL, 1)
        deadline = time.monotonic() + self.erase_timeout
        while True:
            status = (self._ap_read(
                jlink, CTRL_AP, CTRL_AP_ERASEALLSTATUS) & 0x3)
            if status != ERASEALL_BUSY:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('nRF7120 CTRL-AP ERASEALL timed out')
            time.sleep(0.1)

        if status == ERASEALL_READY_TO_RESET:
            self._ap_write(
                jlink, CTRL_AP, CTRL_AP_RESET, CTRL_AP_HARD_RESET)
            time.sleep(0.001)
            self._ap_write(
                jlink, CTRL_AP, CTRL_AP_RESET, CTRL_AP_NO_RESET)
            time.sleep(0.1)
        elif status != ERASEALL_READY:
            name = 'error' if status == ERASEALL_ERROR else f'status {status}'
            raise RuntimeError(f'nRF7120 CTRL-AP ERASEALL failed: {name}')

    @staticmethod
    def _read32(jlink, address):
        return jlink.memory_read32(address, 1)[0]

    def _mram_state(self, jlink):
        registers = (
            ('READY', MRAMC_READY),
            ('READYNEXT', MRAMC_READYNEXT),
            ('CONFIG', MRAMC_WEN),
            ('CONFIGNVR0', MRAMC_CONFIGNVR0),
            ('CPUWAIT', CPUCONF_CPUWAIT),
        )
        values = []
        for name, address in registers:
            try:
                values.append(f'{name}={self._read32(jlink, address):#010x}')
            except Exception as exc:
                values.append(f'{name}=unavailable({type(exc).__name__})')
        return ', '.join(values)

    def _wait_mram_ready(self, jlink, stage):
        deadline = time.monotonic() + self.erase_timeout
        while True:
            ready = self._read32(jlink, MRAMC_READY)
            if ready & MRAMC_READY_READY:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f'nRF7120 MRAMC did not become ready {stage}: '
                    f'{self._mram_state(jlink)}')
            time.sleep(0.001)

    @staticmethod
    def _aligned_region(jlink, start, data):
        end = start + len(data)
        aligned_start = start & ~0xF
        aligned_end = (end + 0xF) & ~0xF
        prefix = list(jlink.memory_read8(aligned_start,
                                         start - aligned_start))
        suffix = list(jlink.memory_read8(end, aligned_end - end))
        return aligned_start, bytes(prefix) + bytes(data) + bytes(suffix)

    def _program_hex(self, jlink, hex_file):
        image = IntelHex()
        image.loadfile(hex_file, format='hex')
        self.logger.info(f'Programming and verifying {hex_file}')

        for start, end in image.segments():
            original = bytes(image.tobinarray(start=start, end=end - 1))
            aligned_start, data = self._aligned_region(
                jlink, start, original)
            words = [
                int.from_bytes(data[index:index + 4], 'little')
                for index in range(0, len(data), 4)
            ]
            try:
                jlink.memory_write(aligned_start, words, nbits=32)
                jlink.memory_write32(aligned_start, [words[0]])
            except Exception as exc:
                raise RuntimeError(
                    f'programming failed for {hex_file} at '
                    f'{aligned_start:#010x}: {self._mram_state(jlink)}') from exc

            self._wait_mram_ready(
                jlink, f'after writing {hex_file} at {aligned_start:#010x}')

            actual = bytes(jlink.memory_read8(start, len(original)))
            if actual != original:
                raise RuntimeError(
                    f'verification failed for {hex_file} at {start:#010x}')

    def _run_process(self, command):
        self.logger.debug('Running command: %s', command)
        result = subprocess.run(
            command, check=False, text=True, capture_output=True)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f'command failed ({result.returncode}): {command}'
                + (f'\n{detail}' if detail else ''))
        return result.stdout

    def _nrfutil_selection(self, serial):
        return [
            '--serial-number', str(serial),
            '--x-partno', hex(NRF7120_PARTNO),
        ]

    def _nrfutil_device_version(self):
        output = self._run_process([self.nrfutil, 'device', '--version'])
        match = re.search(r'nrfutil-device\s+(\d+\.\d+\.\d+)', output)
        if match is None:
            raise RuntimeError(
                f'could not determine nrfutil-device version from: {output}')
        return match.group(1)

    def _require_test_lcs_on_probe(self, serial):
        jlink = pylink.JLink()
        try:
            jlink.open(serial_no=serial)
            jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
            jlink.coresight_configure()
            self._require_test_lcs(jlink)
        finally:
            if jlink.opened():
                jlink.close()

    def _erase_and_release_cpuwait(self, serial):
        selection = self._nrfutil_selection(serial)
        self._run_process([
            self.nrfutil, 'device', 'erase', '--all', *selection])
        self._run_process([
            self.nrfutil, 'device', 'x-write', '--direct',
            '--address', f'{CPUCONF_CPUWAIT:#010x}',
            '--value', '0x00000000', *selection])

    def _batch_operations(self):
        operations = []
        for hex_file in self.hex_files:
            image = IntelHex()
            image.loadfile(hex_file, format='hex')
            self.logger.info(f'Programming and verifying {hex_file}')
            for start, end in image.segments():
                data = list(image.tobinarray(start=start, end=end - 1))
                operations.append({
                    'core': NRFUTIL_CORE_APPLICATION,
                    'operation': {
                        'address': hex(start),
                        'data': data,
                        'direct': False,
                        'type': 'memory-write',
                    },
                })
        return operations

    def _write_batch(self, path, version):
        batch = {
            'nrfutil_device_version': version,
            'device_detection_override': {
                'PartNo': {'partno': NRF7120_PARTNO},
            },
            'operations': self._batch_operations(),
        }
        path.write_text(json.dumps(batch))

    def _program_with_nrfutil(self, serial, directory):
        batch_path = directory / 'program.json'
        self._write_batch(batch_path, self._nrfutil_device_version())
        self._run_process([
            self.nrfutil, 'device', 'batch-execute',
            '--batch-file', os.fspath(batch_path),
            '--serial-number', str(serial),
        ])

    def _verify_with_nrfutil(self, serial, directory):
        selection = self._nrfutil_selection(serial)
        read_index = 0
        for hex_file in self.hex_files:
            expected = IntelHex()
            expected.loadfile(hex_file, format='hex')
            for start, end in expected.segments():
                readback_path = directory / f'readback-{read_index}.hex'
                read_index += 1
                self._run_process([
                    self.nrfutil, 'device', 'x-read', '--direct',
                    '--address', hex(start), '--bytes', str(end - start),
                    '--width', '8', '--to-file', os.fspath(readback_path),
                    *selection,
                ])
                actual = IntelHex()
                actual.loadfile(readback_path, format='hex')
                expected_data = bytes(
                    expected.tobinarray(start=start, end=end - 1))
                actual_data = bytes(
                    actual.tobinarray(start=start, end=end - 1))
                if actual_data != expected_data:
                    mismatch = next(
                        index for index, values in enumerate(
                            zip(actual_data, expected_data, strict=True))
                        if values[0] != values[1]
                    )
                    raise RuntimeError(
                        f'verification failed for {hex_file} at '
                        f'{start + mismatch:#010x}')

    def _start_cpu_on_probe(self, serial):
        jlink = pylink.JLink()
        try:
            jlink.open(serial_no=serial)
            jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
            jlink.coresight_configure()
            jlink.exec_command('CORESIGHT_SetIndexAHBAPToUse=0')
            jlink.exec_command('SetRestartOnClose=0')
            jlink.connect('CORTEX-M33', speed=self.speed, verbose=False)
            self._start_cpu(jlink)
        finally:
            if jlink.opened():
                jlink.close()

    def _start_cpu(self, jlink):
        if self.startup is None:
            raise RuntimeError(
                'no valid application vector table was found')

        vtor, sp, pc = self.startup
        waitstates = (MRAMC_WAITSTATES_KEY << 16) | self.mram_waitstates
        writes = [
            (CPUCONF_CPUSTART, 1),
            (DHCSR, DHCSR_HALT),
            (DEMCR, DEMCR_VC_CORERESET),
            (CPUCONF_CPUWAIT, 0),
            (MRAMC_WAITSTATES, waitstates),
            (SCB_VTOR, vtor),
            (DCRDR, sp),
            (DCRSR, DCRSR_WRITE | REGSEL_MSP),
            (DCRDR, pc),
            (DCRSR, DCRSR_WRITE | REGSEL_PC),
            (DCRDR, XPSR_THUMB),
            (DCRSR, DCRSR_WRITE | REGSEL_XPSR),
            (DEMCR, 0),
            (DHCSR, DHCSR_RESUME),
        ]
        for address, value in writes:
            jlink.memory_write32(address, [value])

    def _flash(self):
        if pylink is None:
            raise RuntimeError(
                'Python dependency pylink-square is required; '
                'install it with pip')
        if IntelHex is None:
            raise RuntimeError('Python dependency intelhex is required')

        self.require(self.nrfutil)
        serial = self._select_probe(pylink.JLink())
        self._require_test_lcs_on_probe(serial)

        with tempfile.TemporaryDirectory(
                prefix='nrf7120-pdk-') as temporary:
            directory = Path(temporary)
            self._erase_and_release_cpuwait(serial)
            self._program_with_nrfutil(serial, directory)
            self._verify_with_nrfutil(serial, directory)

        self._start_cpu_on_probe(serial)

    def do_run(self, command, **kwargs):
        if command != 'flash':
            raise ValueError(f'unsupported command: {command}')
        self._validate_and_collect()

        # Sysbuild supplies --reset only to the final domain for this runner.
        if not self.finish:
            return

        if self.dry_run:
            self.logger.info(
                f'Would program {len(self.hex_files)} image(s) with managed '
                'nrfutil writes and start CPUAPP through PyLink')
            return
        self._flash()
