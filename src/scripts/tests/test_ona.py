#  Copyright 2015 Observable Networks
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import print_function, unicode_literals

import io
import platform

from datetime import datetime
from os.path import join
from shutil import rmtree
from tempfile import mkdtemp
from unittest import TestCase

from mock import patch, MagicMock

from ona_service.ona import ONA
from ona_service.utils import utc


class ONAServiceTests(TestCase):
    def setUp(self):
        self.temp_dir = mkdtemp()

        # Touch a new auto configuration file
        self.auto_config_path = join(self.temp_dir, 'config.auto')
        with io.open(self.auto_config_path, 'wt'):
            pass

    def tearDown(self):
        rmtree(self.temp_dir)

    def _get_instance(self, update_only, env=None):
        env = env or {}
        with patch.dict('ona_service.ona.environ', env):
            ona = ONA(
                config_file=self.auto_config_path,
                poll_seconds=0,
                update_only=update_only,
            )
            ona.api = MagicMock()
            ona.api.proxy_uri = 'https://127.0.0.1:8080'
            ona.api.ona_name = 'ona-test-01'

            return ona

    def test_update_only(self):
        # On the first "update only" run, stats should be sent to the site
        instance = self._get_instance(update_only=True)
        now = datetime.now(utc)
        instance.execute(now=now)
        instance.api.send_signal.assert_called_once_with(
            'sensors',
            data={
                'last_start': now.isoformat(),
                'platform': platform.platform(),
                'python_version': platform.python_version(),
                'ona_version': 'unknown',
                'config_mode': 'manual',
            }
        )
        self.assertTrue(instance.stop_event.is_set())

    def test_update_only_reload(self):
        # On the first "update only" run, stats should be sent to the site.
        # We should reload, since the site had something for us to change.
        env = {'OBSRVBL_MANAGE_MODE': 'auto'}
        instance = self._get_instance(update_only=True, env=env)
        instance.api.get_data.return_value.json.return_value = {
            'config': {'pdns_pps_limit': 102},
        }
        now = datetime.now(utc)
        with self.assertRaises(SystemExit):
            instance.execute(now=now)

        instance.api.send_signal.assert_called_once_with(
            'sensors',
            data={
                'last_start': now.isoformat(),
                'platform': platform.platform(),
                'python_version': platform.python_version(),
                'ona_version': 'unknown',
                'config_mode': 'auto',
            }
        )

    def test_manual_config(self):
        # If configuration mode is not "auto", we shouldn't check for updates
        env = {'OBSRVBL_MANAGE_MODE': 'manual'}
        instance = self._get_instance(update_only=False, env=env)
        instance.execute()
        instance.api.get_data.assert_not_called()

    def test_auto_config(self):
        # If configuration mode is not "auto", we should check for updates
        env = {'OBSRVBL_MANAGE_MODE': 'auto'}
        instance = self._get_instance(update_only=False, env=env)
        instance.execute()
        instance.api.get_data.assert_any_call('sensors/ona-test-01')

    def test_auto_config_reload(self):
        # If the configuration changes we should expect an exit
        env = {'OBSRVBL_MANAGE_MODE': 'auto'}
        instance = self._get_instance(update_only=False, env=env)
        instance.api.get_data.return_value.json.return_value = {
            'config': {
                'host': '127.0.0.1',
                'pdns_pps_limit': 102,
            },
        }
        with self.assertRaises(SystemExit):
            instance.execute()

        # The parameters from the host should be saved
        with io.open(self.auto_config_path, 'rt') as infile:
            actual = infile.read().splitlines()
            expected = [
                'OBSRVBL_HOST="https://127.0.0.1"',
                'OBSRVBL_PDNS_PPS_LIMIT="102"'
            ]
            self.assertEqual(actual, expected)

    def test_config_auto_rules(self):
        env = {'OBSRVBL_MANAGE_MODE': 'auto'}
        instance = self._get_instance(update_only=False, env=env)
        instance.api.get_data.return_value.json.return_value = {
            'config': {
                # Valid rules
                "ipfix_probe_4_type": "netflow-v9",
                "ipfix_probe_4_port": "9996",
                "PNA_SERVICE": True,
                "HOSTNAME_RESOLVER": False,
                "pdns_pps_limit": 102,
                "SERVICE_KEY": "MyServiceKey",
                "networks": "10.0.0.0/8\r\n172.16.0.0/12\r\n192.168.0.0/16",
                # Incorrect IPFIX rule
                "ipfix_probe_4_bogus_thing": "yolo",
                # Non-whitelisted setting
                "mykey": "myvalue",
                # Non-whitelisted character
                "snmp_server": '";sudo adduser evil',
            }
        }
        with self.assertRaises(SystemExit):
            instance.execute()

        with io.open(self.auto_config_path, 'rt') as infile:
            actual = infile.read().splitlines()
            expected = [
                'OBSRVBL_IPFIX_PROBE_4_TYPE="netflow-v9"',
                'OBSRVBL_IPFIX_PROBE_4_PORT="9996"',
                'OBSRVBL_PNA_SERVICE="true"',
                'OBSRVBL_HOSTNAME_RESOLVER="false"',
                'OBSRVBL_PDNS_PPS_LIMIT="102"',
                'OBSRVBL_SERVICE_KEY="MyServiceKey"',
                'OBSRVBL_NETWORKS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"',
            ]
            self.assertItemsEqual(actual, expected)

    @patch('ona_service.ona.listdir', autospec=True)
    def test_watch_ifaces_enabled(self, mock_listdir):
        # We're watching interfaces, and when we check nothing has changed
        env = {'OBSRVBL_WATCH_IFACES': 'true'}
        mock_listdir.return_value = ['eth0']
        instance = self._get_instance(update_only=False, env=env)
        instance.execute()

        # Now something has changed, so we should reload
        mock_listdir.return_value = ['eth0', 'eth1']
        with self.assertRaises(SystemExit):
            instance.execute()

    @patch('ona_service.ona.listdir', autospec=True)
    def test_watch_ifaces_disabled(self, mock_listdir):
        # We're not watching interfaces, so nothing should reload
        mock_listdir.return_value = ['eth0']
        instance = self._get_instance(update_only=False)
        instance.execute()

        # We shouldn't reload, even though the interfaces have changed
        mock_listdir.return_value = ['eth0', 'eth1']
        instance.execute()

        # In fact, we shouldn't have even checked
        mock_listdir.assert_not_called()

    def test_no_host(self):
        # The server doesn't know the sensor
        def _get_data(endpoint):
            ret = MagicMock()
            ret.json.return_value = {'error': 'unknown identity'}
            return ret

        # Therefore there's no reload
        env = {'OBSRVBL_MANAGE_MODE': 'auto'}
        instance = self._get_instance(update_only=False, env=env)
        instance.api.get_data.side_effect = _get_data
        instance.execute()
