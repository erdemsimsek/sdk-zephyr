# Copyright (c) 2023 Nordic Semiconductor ASA.
#
# SPDX-License-Identifier: Apache-2.0

'''Runner for flashing with nrfutil.'''

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from runners.nrf_common import NrfBinaryRunner

# nrfutil error reported when it cannot reach the SWD debug port.
DP_UNREACHABLE_MSG = 'debug port SELECT register failed'


class NrfUtilBinaryRunner(NrfBinaryRunner):
    '''Runner front-end for nrfutil.'''

    def __init__(self, cfg, family, softreset, pinreset, dev_id, erase=False,
                 erase_mode=None, ext_erase_mode=None, reset=True, tool_opt=None,
                 force=False, recover=False, ext_mem_config_file=None,
                 dry_run=False):

        super().__init__(cfg, family, softreset, pinreset, dev_id, erase,
                         erase_mode, ext_erase_mode, reset, tool_opt, force,
                         recover, dry_run)

        self.ext_mem_config_file = ext_mem_config_file

        self._ops = []
        self._op_id = 1

    @classmethod
    def name(cls):
        return 'nrfutil'

    @classmethod
    def capabilities(cls):
        return NrfBinaryRunner._capabilities(mult_dev_ids=True, dry_run=True)

    @classmethod
    def dev_id_help(cls) -> str:
        return NrfBinaryRunner._dev_id_help() + \
               '''.\n This option can be specified multiple times'''

    @classmethod
    def tool_opt_help(cls) -> str:
        return 'Additional options for nrfutil, e.g. "--log-level"'

    @classmethod
    def do_create(cls, cfg, args):
        return NrfUtilBinaryRunner(cfg, args.nrf_family, args.softreset,
                                   args.pinreset, args.dev_id, erase=args.erase,
                                   erase_mode=args.erase_mode,
                                   ext_erase_mode=args.ext_erase_mode,
                                   reset=args.reset, tool_opt=args.tool_opt,
                                   force=args.force, recover=args.recover,
                                   ext_mem_config_file=args.ext_mem_config_file,
                                   dry_run=args.dry_run)

    @classmethod
    def do_add_parser(cls, parser):
        super().do_add_parser(parser)
        parser.add_argument('--ext-mem-config-file', required=False,
                            dest='ext_mem_config_file',
                            help='path to an JSON file with external memory configuration')

    def _exec(self, args, force=False):
        jout_all = []

        cmd = ['nrfutil', '--json', 'device'] + args
        self._log_cmd(cmd)

        if self.dry_run and not force:
            return {}

        with subprocess.Popen(cmd, stdout=subprocess.PIPE) as p:
            for line in iter(p.stdout.readline, b''):
                # https://github.com/ndjson/ndjson-spec
                jout = json.loads(line.decode(sys.getdefaultencoding()))
                jout_all.append(jout)

                if 'x-execute-batch' in args:
                    if jout['type'] == 'batch_update':
                        pld = jout['data']['data']
                        if (
                            pld['type'] == 'task_progress' and
                            pld['data']['progress']['progressPercentage'] == 0
                        ):
                            self.logger.info(pld['data']['progress']['description'])
                    elif jout['type'] == 'batch_end' and jout['data']['error']:
                        raise subprocess.CalledProcessError(
                            jout['data']['error']['code'], cmd,
                            output=jout['data'].get('message', '')
                        )
        if p.returncode != 0:
            raise subprocess.CalledProcessError(p.returncode, cmd)

        return jout_all

    def do_get_boards(self):
        out = self._exec(['list'])
        devs = []
        for o in out:
            if o['type'] == 'task_end':
                devs = o['data']['data']['devices']
        snrs = [dev['serialNumber'] for dev in devs if dev['traits']['jlink']]

        self.logger.debug(f'Found boards: {snrs}')
        return snrs

    def do_require(self):
        self.require('nrfutil')

    def _insert_op(self, op):
        op['operationId'] = f'{self._op_id}'
        self._op_id += 1
        self._ops.append(op)

    def _format_dev_ids(self):
        if isinstance(self.dev_id, list):
            return ','.join(self.dev_id)
        else:
            return self.dev_id

    def _append_batch(self, op, json_file):
        _op = op['operation']
        op_type = _op['type']

        cmd = [f'{op_type}']

        if op_type == 'program':
            cmd += ['--firmware', _op['firmware']['file']]
            opts = _op['options']
            # populate the options
            cmd.append('--options')
            cli_opts = f"chip_erase_mode={opts['chip_erase_mode']}"
            if opts.get('ext_mem_erase_mode'):
                cli_opts += f",ext_mem_erase_mode={opts['ext_mem_erase_mode']}"
            if opts.get('verify'):
                cli_opts += f",verify={opts['verify']}"
            cmd.append(cli_opts)
        elif op_type == 'reset':
            cmd += ['--reset-kind', _op['kind']]
        elif op_type == 'erase':
            cmd.append(f'--{_op["kind"]}')
        elif op_type == 'x-provision-keys':
            cmd += ['--key-file', _op['keyfile']]

        cmd += ['--core', op['core']] if op.get('core') else []
        cmd += ['--x-family', f'{self.family}']
        cmd += ['--x-append-batch', f'{json_file}']
        self._exec(cmd, force=True)

    def _exec_batch(self):
        # Use x-append-batch to get the JSON from nrfutil itself
        json_file = Path(self.cfg.build_dir) / 'zephyr' / 'generated_nrfutil_batch.json'
        json_file.unlink(missing_ok=True)
        for op in self._ops:
            self._append_batch(op, json_file)

        # reset first in case an exception is thrown
        self._ops = []
        self._op_id = 1
        self.logger.debug(f'Executing batch in: {json_file}')
        precmd = []
        if self.ext_mem_config_file:
            # This needs to be prepended, as it's a global option
            precmd = ['--x-ext-mem-config-file', self.ext_mem_config_file]

        batch_cmd = precmd + ['x-execute-batch', '--batch-path', f'{json_file}',
                              '--serial-number', self._format_dev_ids()]
        try:
            self._exec(batch_cmd)
        except subprocess.CalledProcessError as e:
            if not (self.family == 'nrf71' and DP_UNREACHABLE_MSG in str(e.output or '')
                    and self._wake_debug_port()):
                raise
            self.logger.info('Debug port reachable again, retrying')
            self._exec(batch_cmd)

    def _wake_debug_port(self):
        '''Make the nRF71 SWD debug port reachable for nrfutil again.

        When the firmware leaves an nRF71 SoC in System ON idle with no
        peripheral active, nrfutil can no longer reach the SWD debug port,
        and every pin reset boots straight back into that state.
        A J-Link Commander connect still gets through: it resets the target
        and keeps the debug domain powered.
        Returns True if a connect was attempted for every device.
        '''
        from runners.jlink import JLinkBinaryRunner

        JLinkBinaryRunner.default_jlink()
        from runners.jlink import DEFAULT_JLINK_EXE

        jlink = shutil.which(str(DEFAULT_JLINK_EXE))
        if jlink is None:
            self.logger.warning('nRF71 debug port unreachable and J-Link Commander '
                                f'({DEFAULT_JLINK_EXE}) not found, cannot recover')
            return False

        dev_ids = self.dev_id if isinstance(self.dev_id, list) else [self.dev_id]
        if not all(dev_ids):
            return False

        self.logger.warning('nRF71 debug port unreachable, waking it with J-Link Commander')
        with tempfile.TemporaryDirectory(suffix='nrfutil') as d:
            script = Path(d) / 'connect.jlink'
            script.write_text('connect\nq\n')
            for dev_id in dev_ids:
                cmd = [jlink, '-USB', dev_id, '-device', 'cortex-m33', '-if', 'SWD',
                       '-speed', '1000', '-autoconnect', '1', '-NoGui', '1',
                       '-CommandFile', str(script)]
                self._log_cmd(cmd)
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
        return True

    def do_exec_op(self, op, force=False):
        self.logger.debug(f'Executing op: {op}')
        if force:
            if len(self._ops) != 0:
                raise RuntimeError(f'Forced exec with {len(self._ops)} ops')
            self._insert_op(op)
            self._exec_batch()
            return True
        # Defer by default
        return False

    def flush_ops(self, force=True):
        if not force:
            return
        while self.ops:
            self._insert_op(self.ops.popleft())
        self._exec_batch()
