#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.
#

import collections
import json
import random
import string
import subprocess

from paunch.utils import common
from paunch.utils import systemd


class BaseRunner(object):
    def __init__(self, managed_by, cont_cmd, log=None):
        self.managed_by = managed_by
        self.cont_cmd = cont_cmd
        # Leverage pre-configured logger
        self.log = log or common.configure_logging(__name__)

    @staticmethod
    def execute(cmd, log=None, quiet=False):
        if not log:
            log = common.configure_logging(__name__)
        if not quiet:
            log.debug('$ %s' % ' '.join(cmd))
        subproc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        cmd_stdout, cmd_stderr = subproc.communicate()
        if not quiet:
            log.debug(cmd_stdout)
            log.debug(cmd_stderr)
        return (cmd_stdout.decode('utf-8'),
                cmd_stderr.decode('utf-8'),
                subproc.returncode)

    @staticmethod
    def execute_interactive(cmd, log=None):
        if not log:
            log = common.configure_logging(__name__)
        log.debug('$ %s' % ' '.join(cmd))
        return subprocess.call(cmd)

    def current_config_ids(self):
        # List all config_id labels for managed containers
        # FIXME(bogdando): remove once we have it fixed:
        # https://github.com/containers/libpod/issues/1729
        if self.cont_cmd == 'docker':
            fmt = '{{.Label "config_id"}}'
        else:
            fmt = '{{.Labels.config_id}}'
        cmd = [
            self.cont_cmd, 'ps', '-a',
            '--filter', 'label=managed_by=%s' % self.managed_by,
            '--format', fmt
        ]
        cmd_stdout, cmd_stderr, returncode = self.execute(cmd, self.log)
        if returncode != 0:
            return set()
        return set(cmd_stdout.split())

    def containers_in_config(self, conf_id):
        cmd = [
            self.cont_cmd, 'ps', '-q', '-a',
            '--filter', 'label=managed_by=%s' % self.managed_by,
            '--filter', 'label=config_id=%s' % conf_id
        ]
        cmd_stdout, cmd_stderr, returncode = self.execute(cmd, self.log)
        if returncode != 0:
            return []

        return [c for c in cmd_stdout.split()]

    def image_exist(self, name, quiet=False):
        # the command only exists in podman.
        if self.cont_cmd != 'podman':
            self.log.warning("image_exist isn't supported "
                             "by %s" % self.cont_cmd)
            return 0
        cmd = ['podman', 'image', 'exists', name]
        (cmd_stdout, cmd_stderr, returncode) = self.execute(
            cmd, self.log, quiet)
        return returncode

    def inspect(self, name, output_format=None, o_type='container',
                quiet=False):
        img_exist = self.image_exist(name)
        # We want to verify if the image exists before inspecting it.
        # Context: https://github.com/containers/libpod/issues/1845
        if img_exist != 0:
            return
        cmd = [self.cont_cmd, 'inspect', '--type', o_type]
        if output_format:
            cmd.append('--format')
            cmd.append(output_format)
        cmd.append(name)
        (cmd_stdout, cmd_stderr, returncode) = self.execute(
            cmd, self.log, quiet)
        if returncode != 0:
            return
        try:
            if output_format:
                return cmd_stdout
            else:
                return json.loads(cmd_stdout)[0]
        except Exception as e:
            self.log.error('Problem parsing %s inspect: %s' %
                           (self.cont_cmd, e))

    def unique_container_name(self, container):
        container_name = container
        while self.inspect(container_name, output_format='exists', quiet=True):
            suffix = ''.join(random.choice(
                string.ascii_lowercase + string.digits) for i in range(8))
            container_name = '%s-%s' % (container, suffix)
        return container_name

    def discover_container_name(self, container, cid):
        cmd = [
            self.cont_cmd,
            'ps',
            '-a',
            '--filter',
            'label=container_name=%s' % container,
            '--filter',
            'label=config_id=%s' % cid,
            '--format',
            '{{.Names}}'
        ]
        (cmd_stdout, cmd_stderr, returncode) = self.execute(cmd, self.log)
        if returncode != 0:
            return container
        names = cmd_stdout.split()
        if names:
            return names[0]
        return container

    def delete_missing_configs(self, config_ids):
        if not config_ids:
            config_ids = []

        for conf_id in self.current_config_ids():
            if conf_id not in config_ids:
                self.log.debug('%s no longer exists, deleting containers' %
                               conf_id)
                self.remove_containers(conf_id)

    def list_configs(self):
        configs = collections.defaultdict(list)
        for conf_id in self.current_config_ids():
            for container in self.containers_in_config(conf_id):
                configs[conf_id].append(self.inspect(container))
        return configs

    def container_names(self, conf_id=None):
        # list every container name, and its container_name label
        # FIXME(bogdando): remove once we have it fixed:
        # https://github.com/containers/libpod/issues/1729
        if self.cont_cmd == 'docker':
            fmt = '{{.Label "container_name"}}'
        else:
            fmt = '{{.Labels.container_name}}'
        cmd = [
            self.cont_cmd, 'ps', '-a',
            '--filter', 'label=managed_by=%s' % self.managed_by
        ]
        if conf_id:
            cmd.extend((
                '--filter', 'label=config_id=%s' % conf_id
            ))
        cmd.extend((
            '--format', '{{.Names}} %s' % fmt
        ))
        cmd_stdout, cmd_stderr, returncode = self.execute(cmd, self.log)
        if returncode != 0:
            return
        for line in cmd_stdout.split("\n"):
            if line:
                yield line.split()

    def remove_containers(self, conf_id):
        for container in self.containers_in_config(conf_id):
            self.remove_container(container)

    def remove_container(self, container):
        if self.cont_cmd == 'podman':
            systemd.service_delete(container=container, log=self.log)
        cmd = [self.cont_cmd, 'rm', '-f', container]
        cmd_stdout, cmd_stderr, returncode = self.execute(cmd, self.log)
        if returncode != 0:
            self.log.error('Error removing container: %s' % container)
            self.log.error(cmd_stderr)

    def stop_container(self, container, cont_cmd=None, quiet=False):
        cont_cmd = cont_cmd or self.cont_cmd
        cmd = [cont_cmd, 'stop', container]
        cmd_stdout, cmd_stderr, returncode = self.execute(cmd, quiet=quiet)
        if returncode != 0 and not quiet:
            self.log.error('Error stopping container: %s' % container)
            self.log.error(cmd_stderr)

    def rename_containers(self):
        current_containers = []
        need_renaming = {}
        for entry in self.container_names():
            current_containers.append(entry[0])

            # ignore if container_name label not set
            if len(entry) < 2:
                continue

            # ignore if desired name is already actual name
            if entry[0] == entry[-1]:
                continue

            need_renaming[entry[0]] = entry[-1]

        for current, desired in sorted(need_renaming.items()):
            if desired in current_containers:
                self.log.info('Cannot rename "%s" since "%s" still exists' % (
                    current, desired))
            else:
                self.log.info('Renaming "%s" to "%s"' % (current, desired))
                self.rename_container(current, desired)
                current_containers.append(desired)


class DockerRunner(BaseRunner):

    def __init__(self, managed_by, cont_cmd=None, log=None):
        cont_cmd = cont_cmd or 'docker'
        super(DockerRunner, self).__init__(managed_by, cont_cmd, log)

    def rename_container(self, container, name):
        cmd = [self.cont_cmd, 'rename', container, name]
        cmd_stdout, cmd_stderr, returncode = self.execute(cmd, self.log)
        if returncode != 0:
            self.log.error('Error renaming container: %s' % container)
            self.log.error(cmd_stderr)


class PodmanRunner(BaseRunner):

    def __init__(self, managed_by, cont_cmd=None, log=None):
        cont_cmd = cont_cmd or 'podman'
        super(PodmanRunner, self).__init__(managed_by, cont_cmd, log)

    def rename_container(self, container, name):
        # TODO(emilien) podman doesn't support rename, we'll handle it
        # in paunch itself, probably.
        self.log.warning("container renaming isn't supported by podman")
        pass
