# Copyright (c) 2026 Nordic Semiconductor ASA.
#
# SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from intelhex import IntelHex

from runners.nrf7120_pdk import (
    CM33_AP,
    CTRL_AP,
    CTRL_AP_BOOTSTATUS,
    CTRL_AP_ERASEALL,
    CTRL_AP_ERASEALLSTATUS,
    DHCSR,
    DHCSR_RESUME,
    LCS_TEST,
    MEM_AP_CSW,
    MEM_AP_DRW,
    MEM_AP_TAR,
    MRAMC_READY,
    SCB_VTOR,
    Nrf7120PdkBinaryRunner,
)

SNR = 1052802863


class FakeJLink:
    def __init__(self, serials=(SNR,), lcs=LCS_TEST, mram_ready=1,
                 erase_status_after=1):
        self.serials = serials
        self.erase_status_after = erase_status_after
        self.is_open = False
        self.select = 0
        self.memory = {}
        self.memory_writes = []
        self.events = []
        self.commands = []
        self.batch = None
        self.ap_registers = {
            (CM33_AP, MEM_AP_CSW): 0x40,
            (CTRL_AP, CTRL_AP_BOOTSTATUS): lcs << 12,
            (CTRL_AP, CTRL_AP_ERASEALLSTATUS): 0,
        }
        self._store_word(MRAMC_READY, mram_ready)

    def connected_emulators(self):
        return [SimpleNamespace(SerialNumber=serial) for serial in self.serials]

    def open(self, serial_no=None):
        assert serial_no in self.serials
        self.is_open = True

    def opened(self):
        return self.is_open

    def close(self):
        self.is_open = False

    def set_tif(self, interface):
        pass

    def coresight_configure(self):
        pass

    def exec_command(self, command):
        pass

    def connect(self, device, speed, verbose):
        assert device == 'CORTEX-M33'
        self.events.append(('connect',))

    def halt(self):
        self.events.append(('halt',))
        return True

    def coresight_write(self, reg, data, ap):
        if not ap:
            assert reg == 2
            self.select = data
            return
        ap_index = self.select >> 24
        address = (self.select & 0xF0) | (reg * 4)
        self.events.append(('ap_write', ap_index, address, data))
        self.ap_registers[(ap_index, address)] = data
        if (ap_index, address) == (CTRL_AP, CTRL_AP_ERASEALL):
            self.ap_registers[(CTRL_AP, CTRL_AP_ERASEALLSTATUS)] = (
                self.erase_status_after)
        elif (ap_index, address) == (CM33_AP, MEM_AP_DRW):
            target = self.ap_registers[(CM33_AP, MEM_AP_TAR)]
            self.events.append(('direct_write32', target, data))
            self._store_word(target, data)

    def coresight_read(self, reg, ap):
        assert ap
        ap_index = self.select >> 24
        address = (self.select & 0xF0) | (reg * 4)
        return self.ap_registers.get((ap_index, address), 0)

    def memory_read8(self, address, count):
        return [self.memory.get(address + offset, 0xff)
                for offset in range(count)]

    def memory_read32(self, address, count):
        self.events.append(('memory_read32', address, count))
        return [
            int.from_bytes(
                bytes(self.memory.get(address + index * 4 + offset, 0xff)
                      for offset in range(4)),
                'little',
            )
            for index in range(count)
        ]

    def memory_write8(self, address, values):
        self.memory_writes.append((address, list(values), 8))
        self.events.append(('memory_write', address, tuple(values), 8))
        for offset, value in enumerate(values):
            self.memory[address + offset] = value

    def _store_word(self, address, value):
        for offset, byte in enumerate(value.to_bytes(4, 'little')):
            self.memory[address + offset] = byte

    def memory_write32(self, address, values):
        self.memory_writes.append((address, list(values), 32))
        self.events.append(('memory_write', address, tuple(values), 32))
        for index, value in enumerate(values):
            self._store_word(address + index * 4, value)

    def memory_write(self, address, values, nbits):
        assert nbits == 32
        self.memory_write32(address, values)


@pytest.fixture(autouse=True)
def mock_require(monkeypatch):
    monkeypatch.setattr(
        Nrf7120PdkBinaryRunner, 'require', lambda self, program: None)


def mock_nrfutil(monkeypatch, fake):
    def run(command, **kwargs):
        fake.commands.append(tuple(command))
        stdout = ''

        if command[1:] == ['device', '--version']:
            stdout = 'nrfutil-device 2.19.2 (test)\\n'
        elif 'batch-execute' in command:
            batch_path = Path(command[command.index('--batch-file') + 1])
            fake.batch = json.loads(batch_path.read_text())
            for entry in fake.batch['operations']:
                operation = entry['operation']
                address = int(operation['address'], 0)
                for offset, value in enumerate(operation['data']):
                    fake.memory[address + offset] = value
        elif 'x-read' in command:
            address = int(command[command.index('--address') + 1], 0)
            count = int(command[command.index('--bytes') + 1], 0)
            output = Path(command[command.index('--to-file') + 1])
            image = IntelHex()
            image.puts(
                address,
                bytes(fake.memory.get(address + offset, 0xff)
                      for offset in range(count)),
            )
            image.write_hex_file(output)
        elif 'x-write' in command:
            address = int(command[command.index('--address') + 1], 0)
            value = int(command[command.index('--value') + 1], 0)
            fake._store_word(address, value)

        return SimpleNamespace(returncode=0, stdout=stdout, stderr='')

    monkeypatch.setattr('runners.nrf7120_pdk.subprocess.run', run)


def image_config(runner_config, tmp_path, name, address, data,
                 load_offset=0):
    build_dir = tmp_path / name
    zephyr = build_dir / 'zephyr'
    zephyr.mkdir(parents=True)
    (zephyr / '.config').write_text(f'''
CONFIG_SOC_NRF7120_ENGA_CPUAPP=y
CONFIG_FLASH_BASE_ADDRESS=0x0
CONFIG_FLASH_LOAD_OFFSET={load_offset:#x}
''')
    hex_file = zephyr / 'zephyr.hex'
    image = IntelHex()
    image.puts(address, data)
    image.write_hex_file(hex_file)
    return runner_config._replace(build_dir=os.fspath(build_dir),
                                  hex_file=os.fspath(hex_file))


def test_nrf7120_pdk_programs_verifies_and_starts(runner_config, tmp_path,
                                                   monkeypatch):
    vector = (0x20001948).to_bytes(4, 'little')
    vector += (0x00001395).to_bytes(4, 'little')
    vector += b'\x11\x22\x33\x44'
    cfg = image_config(runner_config, tmp_path, 'app', 0, vector)
    fake = FakeJLink()
    monkeypatch.setattr(
        'runners.nrf7120_pdk.pylink.JLink', lambda: fake)
    mock_nrfutil(monkeypatch, fake)

    runner = Nrf7120PdkBinaryRunner(cfg, dev_id=None, reset=True)
    runner.do_run('flash')

    assert runner.dev_id == str(SNR)
    assert bytes(fake.memory[index]
                 for index in range(len(vector))) == vector
    assert (SCB_VTOR, [0], 32) in fake.memory_writes
    assert (DHCSR, [DHCSR_RESUME], 32) in fake.memory_writes
    assert bytes(fake.memory[index] for index in range(4, 8)) == vector[4:8]
    assert (0x00000004, [1], 32) not in fake.memory_writes
    assert ('direct_write32', 0x00000004, 1) not in fake.events

    erase = next(
        index for index, command in enumerate(fake.commands)
        if 'erase' in command)
    release = next(
        index for index, command in enumerate(fake.commands)
        if 'x-write' in command)
    batch = next(
        index for index, command in enumerate(fake.commands)
        if 'batch-execute' in command)
    verify = next(
        index for index, command in enumerate(fake.commands)
        if 'x-read' in command)
    connect = fake.events.index(('connect',))
    assert erase < release < batch < verify
    assert connect == 0
    assert fake.batch['device_detection_override'] == {
        'PartNo': {'partno': 0x2c}
    }
    assert fake.batch['operations'][0]['operation']['data'] == list(vector)
    assert not fake.opened()


def test_nrf7120_pdk_starts_offset_image(runner_config, tmp_path,
                                          monkeypatch):
    vector = (0x20001948).to_bytes(4, 'little')
    vector += (0x0000b395).to_bytes(4, 'little')
    cfg = image_config(runner_config, tmp_path, 'app', 0xa000, vector,
                       load_offset=0xa000)
    fake = FakeJLink()
    monkeypatch.setattr(
        'runners.nrf7120_pdk.pylink.JLink', lambda: fake)
    mock_nrfutil(monkeypatch, fake)

    runner = Nrf7120PdkBinaryRunner(cfg, dev_id=SNR, reset=True)
    runner.do_run('flash')

    assert runner.startup == (0xa000, 0x20001948, 0x0000b394)
    assert (SCB_VTOR, [0xa000], 32) in fake.memory_writes


def test_nrf7120_pdk_rejects_non_test_lcs(runner_config, tmp_path,
                                           monkeypatch):
    vector = (0x20001948).to_bytes(4, 'little')
    vector += (0x0000b395).to_bytes(4, 'little')
    cfg = image_config(runner_config, tmp_path, 'app', 0xa000, vector,
                       load_offset=0xa000)
    fake = FakeJLink(lcs=0x99CC9)
    monkeypatch.setattr(
        'runners.nrf7120_pdk.pylink.JLink', lambda: fake)

    runner = Nrf7120PdkBinaryRunner(cfg, dev_id=SNR, reset=True)
    with pytest.raises(RuntimeError, match='already be in TEST LCS'):
        runner.do_run('flash')
    assert not fake.opened()


def test_nrf7120_pdk_requires_unique_probe(runner_config, tmp_path,
                                           monkeypatch):
    vector = (0x20001948).to_bytes(4, 'little')
    vector += (0x0000b395).to_bytes(4, 'little')
    cfg = image_config(runner_config, tmp_path, 'app', 0xa000, vector,
                       load_offset=0xa000)
    fake = FakeJLink(serials=(SNR, 123456789))
    monkeypatch.setattr(
        'runners.nrf7120_pdk.pylink.JLink', lambda: fake)

    runner = Nrf7120PdkBinaryRunner(cfg, dev_id=None, reset=True)
    with pytest.raises(RuntimeError, match='multiple J-Link probes'):
        runner.do_run('flash')


def test_nrf7120_pdk_times_out_waiting_for_mram(runner_config):
    fake = FakeJLink(mram_ready=0)
    runner = Nrf7120PdkBinaryRunner(
        runner_config, dev_id=SNR, erase_timeout=0)

    with pytest.raises(RuntimeError, match='MRAMC did not become ready test'):
        runner._wait_mram_ready(fake, 'test')


def test_nrf7120_pdk_collects_sysbuild_images(runner_config, tmp_path):
    vector = (0x20001948).to_bytes(4, 'little')
    vector += (0x0000b395).to_bytes(4, 'little')
    app_cfg = image_config(runner_config, tmp_path, 'app', 0xa000, vector,
                           load_offset=0xa000)
    uicr_cfg = image_config(runner_config, tmp_path, 'uicr',
                            0x00ffd000, b'\x01\x02\x03\x04')
    wicr_cfg = image_config(runner_config, tmp_path, 'wicr',
                            0x003fd000, b'\x05\x06\x07\x08')

    app = Nrf7120PdkBinaryRunner(app_cfg, dev_id=SNR, reset=False)
    app.do_run('flash')
    args = SimpleNamespace(dev_id=None)
    Nrf7120PdkBinaryRunner.args_from_previous_runner(app, args)

    uicr = Nrf7120PdkBinaryRunner(
        uicr_cfg, dev_id=args.dev_id, reset=False,
        hex_files=args.nrf7120_pdk_hex_files,
        startup=args.nrf7120_pdk_startup)
    uicr.do_run('flash')
    Nrf7120PdkBinaryRunner.args_from_previous_runner(uicr, args)

    final = Nrf7120PdkBinaryRunner(
        wicr_cfg, dev_id=args.dev_id, reset=True, dry_run=True,
        hex_files=args.nrf7120_pdk_hex_files,
        startup=args.nrf7120_pdk_startup)
    final.do_run('flash')

    assert final.hex_files == [
        app_cfg.hex_file, uicr_cfg.hex_file, wicr_cfg.hex_file
    ]
    assert final.startup == (0xa000, 0x20001948, 0x0000b394)
