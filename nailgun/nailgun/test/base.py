# -*- coding: utf-8 -*-

#    Copyright 2013 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

try:
    from unittest.case import TestCase
except ImportError:
    # Runing unit-tests in production environment
    from unittest2.case import TestCase

import abc
import mock
import os
import re
import time

from datetime import datetime
from functools import partial
from itertools import izip
from netaddr import IPNetwork
from random import randint

from oslo.serialization import jsonutils

import alembic
import six
import web
from webtest import app

import nailgun
from nailgun.api.v1.urls import urls

from nailgun import consts

from nailgun.db import db
from nailgun.db import dropdb
from nailgun.db import flush
from nailgun.db.migration import ALEMBIC_CONFIG
from nailgun.db import syncdb

from nailgun.logger import logger

from nailgun.db.sqlalchemy.fixman import load_fixture
from nailgun.db.sqlalchemy.fixman import upload_fixture
from nailgun.db.sqlalchemy.models import NodeAttributes
from nailgun.db.sqlalchemy.models import NodeNICInterface
from nailgun.db.sqlalchemy.models import Notification
from nailgun.db.sqlalchemy.models import Task

# here come objects
from nailgun.objects import Cluster
from nailgun.objects import Node
from nailgun.objects import NodeGroup
from nailgun.objects import Release

from nailgun.app import build_app
from nailgun.consts import NETWORK_INTERFACE_TYPES
from nailgun.middleware.keystone import NailgunFakeKeystoneAuthMiddleware
from nailgun.network.manager import NetworkManager


class TimeoutError(Exception):
    pass


def test_db_driver(handler):
    try:
        return handler()
    except web.HTTPError:
        if str(web.ctx.status).startswith(("4", "5")):
            db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.commit()
        # we do not remove session in tests


class EnvironmentManager(object):

    def __init__(self, app, session=None):
        self.db = session or db()
        self.app = app
        self.tester = TestCase
        self.tester.runTest = lambda a: None
        self.tester = self.tester()
        self.here = os.path.abspath(os.path.dirname(__file__))
        self.fixture_dir = os.path.join(self.here, "..", "fixtures")
        self.default_headers = {
            "Content-Type": "application/json"
        }
        self.releases = []
        self.clusters = []
        self.nodes = []
        self.network_manager = NetworkManager

    def create(self, **kwargs):
        release_data = kwargs.pop('release_kwargs', {"api": False})
        cluster_data = kwargs.pop('cluster_kwargs', {})
        if 'release_id' not in cluster_data:
            cluster_data['release_id'] = self.create_release(**release_data).id
        cluster = self.create_cluster(
            **cluster_data
        )
        for node_kwargs in kwargs.pop('nodes_kwargs', []):
            if "cluster_id" not in node_kwargs:
                if isinstance(cluster, dict):
                    node_kwargs["cluster_id"] = cluster["id"]
                else:
                    node_kwargs["cluster_id"] = cluster.id
            node_kwargs.setdefault("api", False)
            if "pending_roles" not in node_kwargs:
                node_kwargs.setdefault("roles", ["controller"])
            self.create_node(
                **node_kwargs
            )
        return cluster

    def create_release(self, api=False, **kwargs):
        os = kwargs.get(
            'operating_system', consts.RELEASE_OS.centos)
        version = kwargs.get(
            'version', '{0}-5.1'.format(randint(0, 100000000)))

        # NOTE(ikalnitsky): In order to do not read each time openstack.yaml
        # we're reading it once and then look for needed release.
        releases = self.read_fixtures(('openstack',))
        release_data = next((
            r for r in releases if r['fields']['operating_system'] == os),
            releases[0])
        release_data = release_data['fields']

        release_data.update({
            'name': u"release_name_" + version,
            'version': version,
            'description': u"release_desc" + version,
            'roles': self.get_default_roles(),
        })

        if kwargs:
            release_data.update(kwargs)
        if api:
            resp = self.app.post(
                reverse('ReleaseCollectionHandler'),
                params=jsonutils.dumps(release_data),
                headers=self.default_headers
            )
            self.tester.assertEqual(resp.status_code, 201)
            release = resp.json_body
            self.releases.append(
                self.db.query(Release).get(release['id'])
            )
        else:
            release = Release.create(release_data)
            db().commit()
            self.releases.append(release)
        return release

    def get_role(self, release_id, role_name, expect_errors=False):
        return self.app.get(
            reverse(
                'RoleHandler',
                {'role_name': role_name, 'release_id': release_id}),
            headers=self.default_headers,
            expect_errors=expect_errors
        )

    def update_role(self, release_id, role_name, data, expect_errors=False):
        return self.app.put(
            reverse(
                'RoleHandler',
                {'role_name': role_name, 'release_id': release_id}),
            jsonutils.dumps(data),
            headers=self.default_headers,
            expect_errors=expect_errors
        )

    def delete_role(self, release_id, role_name, expect_errors=False):
        return self.app.delete(
            reverse(
                'RoleHandler',
                {'role_name': role_name, 'release_id': release_id}),
            headers=self.default_headers,
            expect_errors=expect_errors
        )

    def create_role(self, release_id, data, expect_errors=False):
        return self.app.post(
            reverse('RoleCollectionHandler', {'release_id': release_id}),
            jsonutils.dumps(data),
            headers=self.default_headers,
            expect_errors=expect_errors
        )

    def create_cluster(self, api=True, exclude=None, **kwargs):
        cluster_data = {
            'name': 'cluster-api-' + str(randint(0, 1000000)),
        }
        editable_attributes = kwargs.pop(
            'editable_attributes', None)

        if kwargs:
            cluster_data.update(kwargs)

        if 'release_id' not in cluster_data:
            cluster_data['release_id'] = self.create_release(api=False).id

        if exclude and isinstance(exclude, list):
            for ex in exclude:
                try:
                    del cluster_data[ex]
                except KeyError as err:
                    logger.warning(err)
        if api:
            resp = self.app.post(
                reverse('ClusterCollectionHandler'),
                jsonutils.dumps(cluster_data),
                headers=self.default_headers,
                expect_errors=True
            )
            self.tester.assertEqual(resp.status_code, 201, resp.body)
            cluster = resp.json_body
            cluster_db = Cluster.get_by_uid(cluster['id'])
        else:
            cluster = Cluster.create(cluster_data)
            cluster_db = cluster
            db().commit()
        self.clusters.append(cluster_db)

        if editable_attributes:
            Cluster.patch_attributes(cluster_db,
                                     {'editable': editable_attributes})
        return cluster

    def create_node(
            self, api=False,
            exclude=None, expect_http=201,
            expected_error=None,
            **kwargs):
        #TODO(alekseyk) Simplify 'interfaces' and 'mac' manipulation logic
        metadata = kwargs.get('meta')
        default_metadata = self.default_metadata()
        if metadata:
            default_metadata.update(metadata)
            meta_ifaces = 'interfaces' in metadata

        mac = kwargs.get('mac', self.generate_random_mac())
        if default_metadata['interfaces']:
            default_metadata['interfaces'][0]['mac'] = mac
            if not metadata or not meta_ifaces:
                for iface in default_metadata['interfaces'][1:]:
                    if 'mac' in iface:
                        iface['mac'] = self.generate_random_mac()

        node_data = {
            'mac': mac,
            'status': 'discover',
            'ip': '10.20.0.130',
            'meta': default_metadata
        }
        if kwargs:
            meta = kwargs.pop('meta', None)
            node_data.update(kwargs)
            if meta:
                kwargs['meta'] = meta

        if exclude and isinstance(exclude, list):
            for ex in exclude:
                try:
                    del node_data[ex]
                except KeyError as err:
                    logger.warning(err)
        if api:
            resp = self.app.post(
                reverse('NodeCollectionHandler'),
                jsonutils.dumps(node_data),
                headers=self.default_headers,
                expect_errors=True
            )
            self.tester.assertEqual(resp.status_code, expect_http, resp.body)
            if expected_error:
                self.tester.assertEqual(
                    resp.json_body["message"],
                    expected_error
                )
            if str(expect_http)[0] != "2":
                return None
            self.tester.assertEqual(resp.status_code, expect_http)
            node = resp.json_body
            node_db = Node.get_by_uid(node['id'])
            if 'interfaces' not in node_data['meta'] \
                    or not node_data['meta']['interfaces']:
                self._set_interfaces_if_not_set_in_meta(
                    node_db.id,
                    kwargs.get('meta', None))
            self.nodes.append(node_db)
        else:
            node = Node.create(node_data)
            db().commit()
            self.nodes.append(node)

        return node

    def create_nodes(self, count, *args, **kwargs):
        """Helper to generate specific number of nodes."""
        return [self.create_node(*args, **kwargs) for _ in xrange(count)]

    def create_nodes_w_interfaces_count(self,
                                        nodes_count, if_count=2, **kwargs):
        """Create nodes_count nodes with if_count interfaces each.
        Default random MAC is generated for each interface.
        """
        nodes = []
        for i in range(nodes_count):
            meta = self.default_metadata()
            if_list = [
                {
                    "name": "eth{0}".format(i),
                    "mac": self.generate_random_mac()
                }
                for i in range(if_count)]
            self.set_interfaces_in_meta(meta, if_list)
            nodes.append(self.create_node(meta=meta, **kwargs))
        return nodes

    def create_task(self, **kwargs):
        task = Task(**kwargs)
        self.db.add(task)
        self.db.commit()
        return task

    def create_attributes(self):
        return NodeAttributes()

    def create_notification(self, **kwargs):
        notif_data = {
            "topic": "discover",
            "message": "Test message",
            "status": "unread",
            "datetime": datetime.now()
        }
        if kwargs:
            notif_data.update(kwargs)
        notification = Notification()
        notification.cluster_id = notif_data.get("cluster_id")
        for f, v in notif_data.iteritems():
            setattr(notification, f, v)
        self.db.add(notification)
        self.db.commit()
        return notification

    def create_node_group(self, api=True, **kwargs):
        ng_data = {
            'cluster_id': self.clusters[0].id,
            'name': 'test_ng'
        }
        if kwargs:
            ng_data.update(kwargs)
        if api:
            resp = self.app.post(
                reverse('NodeGroupCollectionHandler'),
                jsonutils.dumps(ng_data),
                headers=self.default_headers,
                expect_errors=False
            )

            ng = resp
        else:
            ng = NodeGroup.create(ng_data)
            db().commit()

        return ng

    def default_metadata(self):
        item = self.find_item_by_pk_model(
            self.read_fixtures(("sample_environment",)),
            1, 'nailgun.node')
        return item.get('fields').get('meta')

    def generate_random_mac(self):
        mac = [randint(0x00, 0x7f) for _ in xrange(6)]
        return ':'.join(map(lambda x: "%02x" % x, mac)).lower()

    def generate_interfaces_in_meta(self, amount):
        nics = []
        for i in xrange(amount):
            nics.append(
                {
                    'name': 'eth{0}'.format(i),
                    'mac': self.generate_random_mac(),
                    'current_speed': 100,
                    'max_speed': 1000
                }
            )
        self.set_admin_ip_for_for_single_interface(nics)
        return {'interfaces': nics}

    def _set_interfaces_if_not_set_in_meta(self, node_id, meta):
        if not meta or not 'interfaces' in meta:
            self._add_interfaces_to_node(node_id)

    def _create_interfaces_from_meta(self, node):
        # Create interfaces from meta
        for interface in node.meta['interfaces']:
            interface = NodeNICInterface(
                mac=interface.get('mac'),
                name=interface.get('name'),
                ip_addr=interface.get('ip'),
                netmask=interface.get('netmask')
            )
            self.db.add(interface)
            node.nic_interfaces.append(interface)

        self.db.flush()
        # If node in a cluster then assign networks for all interfaces
        if node.cluster_id:
            self.network_manager.assign_networks_by_default(node)
        # At least one interface should have
        # same ip as mac in meta
        if node.nic_interfaces and not \
           filter(lambda i: node.mac == i.mac, node.nic_interfaces):

            node.nic_interfaces[0].mac = node.mac
        self.db.commit()

    def _add_interfaces_to_node(self, node_id, count=1):
        interfaces = []
        node = self.db.query(Node).get(node_id)
        networks_to_assign = \
            list(node.cluster.network_groups) if node.cluster else []

        for i in xrange(count):
            interface = NodeNICInterface({
                'node_id': node_id,
                'name': 'eth{0}'.format(i),
                'mac': self.generate_random_mac(),
                'current_speed': 100,
                'max_speed': 1000,
                'assigned_networks': networks_to_assign
            })
            self.db.add(interface)
            self.db.commit()

            interfaces.append(interface)
            # assign all networks to first NIC
            networks_to_assign = []

        return interfaces

    def set_admin_ip_for_for_single_interface(self, interfaces):
        """Set admin ip for single interface if it not setted yet."""
        ips = [interface.get('ip') for interface in interfaces]
        admin_ips = [
            ip for ip in ips
            if self.network_manager.is_ip_belongs_to_admin_subnet(ip)]

        if not admin_ips:
            admin_cidr = self.network_manager.get_admin_network_group().cidr
            interfaces[0]['ip'] = str(IPNetwork(admin_cidr).ip)

    def set_interfaces_in_meta(self, meta, interfaces):
        """Set interfaces in metadata."""
        meta['interfaces'] = interfaces
        self.set_admin_ip_for_for_single_interface(meta['interfaces'])
        return meta['interfaces']

    def get_default_roles(self):
        return ['controller', 'compute', 'cinder', 'ceph-osd', 'mongo']

    def get_default_volumes_metadata(self):
        return self.read_fixtures(
            ('openstack',))[0]['fields']['volumes_metadata']

    def get_default_roles_metadata(self):
        return self.read_fixtures(
            ('openstack',))[0]['fields']['roles_metadata']

    def get_default_networks_metadata(self):
        return self.read_fixtures(
            ('openstack',))[0]['fields']['networks_metadata']

    def get_default_attributes_metadata(self):
        return self.read_fixtures(
            ['openstack'])[0]['fields']['attributes_metadata']

    def get_default_plugin_env_config(self, **kwargs):
        return {
            'attributes': {
                '{0}_text'.format(kwargs.get('plugin_name', 'plugin_name')): {
                    'value': kwargs.get('value', 'value'),
                    'type': kwargs.get('type', 'type'),
                    'description': kwargs.get('description', 'description'),
                    'weight': kwargs.get('weight', 25),
                    'label': kwargs.get('label', 'label')}}}

    def get_default_plugin_metadata(self, **kwargs):
        sample_plugin = {
            'version': '0.1.0',
            'name': 'testing_plugin',
            'title': 'Test plugin',
            'package_version': '1.0.0',
            'description': 'Enable to use plugin X for Neutron',
            'fuel_version': ['6.0'],
            'groups': [],
            'licenses': ['License 1'],
            'authors': ['Author1'],
            'homepage': 'http://some-plugin-url.com/',
            'releases': [
                {'repository_path': 'repositories/ubuntu',
                 'version': '2014.2-6.0', 'os': 'ubuntu',
                 'mode': ['ha', 'multinode'],
                 'deployment_scripts_path': 'deployment_scripts/'},
                {'repository_path': 'repositories/centos',
                 'version': '2014.2-6.0', 'os': 'centos',
                 'mode': ['ha', 'multinode'],
                 'deployment_scripts_path': 'deployment_scripts/'}]}

        sample_plugin.update(kwargs)
        return sample_plugin

    def get_default_vmware_attributes_metadata(self):
        return self.read_fixtures(
            ['openstack'])[0]['fields']['vmware_attributes_metadata']

    def upload_fixtures(self, fxtr_names):
        for fxtr_path in self.fxtr_paths_by_names(fxtr_names):
            with open(fxtr_path, "r") as fxtr_file:
                upload_fixture(fxtr_file)

    def read_fixtures(self, fxtr_names):
        data = []
        for fxtr_path in self.fxtr_paths_by_names(fxtr_names):
            with open(fxtr_path, "r") as fxtr_file:
                try:
                    data.extend(load_fixture(fxtr_file))
                except Exception as exc:
                    logger.error(
                        'Error "%s" occurred while loading '
                        'fixture %s' % (exc, fxtr_path)
                    )
        return data

    def fxtr_paths_by_names(self, fxtr_names):
        for fxtr in fxtr_names:
            for ext in ['json', 'yaml']:
                fxtr_path = os.path.join(
                    self.fixture_dir,
                    "%s.%s" % (fxtr, ext)
                )

                if os.path.exists(fxtr_path):
                    logger.debug(
                        "Fixture file is found, yielding path: %s",
                        fxtr_path
                    )
                    yield fxtr_path
                    break
            else:
                logger.warning(
                    "Fixture file was not found: %s",
                    fxtr
                )

    def find_item_by_pk_model(self, data, pk, model):
        for item in data:
            if item.get('pk') == pk and item.get('model') == model:
                return item

    def launch_provisioning_selected(self, nodes_uids=None):
        if self.clusters:
            if not nodes_uids:
                nodes_uids = [n.uid for n in self.clusters[0].nodes]
            action_url = reverse(
                'ProvisionSelectedNodes',
                kwargs={'cluster_id': self.clusters[0].id}
            ) + '?nodes={0}'.format(','.join(nodes_uids))
            resp = self.app.put(
                action_url,
                '{}',
                headers=self.default_headers,
                expect_errors=True
            )
            self.tester.assertEqual(202, resp.status_code)
            response = resp.json_body
            return self.db.query(Task).filter_by(
                uuid=response['uuid']
            ).first()
        else:
            raise NotImplementedError(
                "Nothing to provision - try creating cluster"
            )

    def launch_deployment(self):
        if self.clusters:
            resp = self.app.put(
                reverse(
                    'ClusterChangesHandler',
                    kwargs={'cluster_id': self.clusters[0].id}),
                headers=self.default_headers)

            return self.db.query(Task).filter_by(
                uuid=resp.json_body['uuid']
            ).first()
        else:
            raise NotImplementedError(
                "Nothing to deploy - try creating cluster"
            )

    def stop_deployment(self):
        if self.clusters:
            resp = self.app.put(
                reverse(
                    'ClusterStopDeploymentHandler',
                    kwargs={'cluster_id': self.clusters[0].id}),
                expect_errors=True,
                headers=self.default_headers)

            return self.db.query(Task).filter_by(
                uuid=resp.json_body['uuid']
            ).first()
        else:
            raise NotImplementedError(
                "Nothing to stop - try creating cluster"
            )

    def reset_environment(self, expect_http=202):
        if self.clusters:
            resp = self.app.put(
                reverse(
                    'ClusterResetHandler',
                    kwargs={'cluster_id': self.clusters[0].id}),
                expect_errors=True,
                headers=self.default_headers)
            self.tester.assertEqual(resp.status_code, expect_http)
            if not str(expect_http).startswith("2"):
                return resp.body
            return self.db.query(Task).filter_by(
                uuid=resp.json_body['uuid']
            ).first()
        else:
            raise NotImplementedError(
                "Nothing to reset - try creating cluster"
            )

    def delete_environment(self, expect_http=202):
        if self.clusters:
            resp = self.app.delete(
                reverse(
                    'ClusterHandler',
                    kwargs={'obj_id': self.clusters[0].id}),
                expect_errors=True,
                headers=self.default_headers)
            self.tester.assertEqual(resp.status_code, expect_http)
            if not str(expect_http).startswith("2"):
                return resp.body
            return self.db.query(Task).filter_by(
                name=consts.TASK_NAMES.cluster_deletion
            ).first()
        else:
            raise NotImplementedError(
                "Nothing to delete - try creating cluster"
            )

    def update_environment(self, pending_release_id=None, expect_http=202):
        if self.clusters:
            if not pending_release_id:
                pending_release_id = self.clusters[0].release_id
            self.clusters[0].pending_release_id = pending_release_id
            self.db.commit()
            resp = self.app.put(
                reverse(
                    'ClusterUpdateHandler',
                    kwargs={'cluster_id': self.clusters[0].id}),
                expect_errors=True,
                headers=self.default_headers)
            self.tester.assertEqual(expect_http, resp.status_code)
            if not str(expect_http).startswith("2"):
                return resp.body
            return self.db.query(Task).filter_by(
                name=consts.TASK_NAMES.update
            ).first()
        else:
            raise NotImplementedError(
                "Nothing to update - try creating cluster"
            )

    def launch_verify_networks(self, data=None):
        if self.clusters:
            net_urls = {
                "nova_network": {
                    "config": "NovaNetworkConfigurationHandler",
                    "verify": "NovaNetworkConfigurationVerifyHandler"
                },
                "neutron": {
                    "config": "NeutronNetworkConfigurationHandler",
                    "verify": "NeutronNetworkConfigurationVerifyHandler"
                }
            }
            provider = self.clusters[0].net_provider
            if data:
                nets = jsonutils.dumps(data)
            else:
                resp = self.app.get(
                    reverse(
                        net_urls[provider]["config"],
                        kwargs={'cluster_id': self.clusters[0].id}
                    ),
                    headers=self.default_headers
                )
                self.tester.assertEqual(200, resp.status_code)
                nets = resp.body

            resp = self.app.put(
                reverse(
                    net_urls[provider]["verify"],
                    kwargs={'cluster_id': self.clusters[0].id}),
                nets,
                headers=self.default_headers
            )
            task_uuid = resp.json_body['uuid']
            return self.db.query(Task).filter_by(uuid=task_uuid).first()
        else:
            raise NotImplementedError(
                "Nothing to verify - try creating cluster"
            )

    def make_bond_via_api(self, bond_name, bond_mode, nic_names, node_id=None,
                          bond_properties=None, interface_properties=None):
        if not node_id:
            node_id = self.nodes[0]["id"]
        resp = self.app.get(
            reverse("NodeNICsHandler",
                    kwargs={"node_id": node_id}),
            headers=self.default_headers)
        self.tester.assertEqual(resp.status_code, 200)
        data = resp.json_body

        nics = self.db.query(NodeNICInterface).filter(
            NodeNICInterface.name.in_(nic_names)
        ).filter(
            NodeNICInterface.node_id == node_id
        )
        self.tester.assertEqual(nics.count(), len(nic_names))

        assigned_nets, slaves = [], []
        for nic in data:
            if nic['name'] in nic_names:
                assigned_nets.extend(nic['assigned_networks'])
                slaves.append({'name': nic['name']})
                nic['assigned_networks'] = []
        bond_dict = {
            "name": bond_name,
            "type": NETWORK_INTERFACE_TYPES.bond,
            "mode": bond_mode,
            "slaves": slaves,
            "assigned_networks": assigned_nets
        }
        if bond_properties:
            bond_dict["bond_properties"] = bond_properties
        if interface_properties:
            bond_dict["interface_properties"] = interface_properties
        data.append(bond_dict)
        resp = self.node_nics_put(node_id, data)
        self.tester.assertEqual(resp.status_code, 200)

    def refresh_nodes(self):
        for n in self.nodes[:]:
            try:
                self.db.add(n)
                self.db.refresh(n)
            except Exception:
                self.nodes.remove(n)
        self.db.flush()

    def refresh_clusters(self):
        for n in self.clusters[:]:
            try:
                self.db.refresh(n)
            except Exception:
                self.nodes.remove(n)

    def _wait_task(self, task, timeout, message):
        timer = time.time()
        while task.status == 'running':
            self.db.refresh(task)
            if time.time() - timer > timeout:
                raise Exception(
                    "Task '{0}' seems to be hanged".format(
                        task.name
                    )
                )
            time.sleep(1)
        self.tester.assertEqual(task.progress, 100)
        if isinstance(message, type(re.compile("regexp"))):
            self.tester.assertIsNotNone(re.match(message, task.message))
        elif isinstance(message, str):
            self.tester.assertEqual(task.message, message)

    def wait_ready(self, task, timeout=60, message=None):
        self._wait_task(task, timeout, message)
        self.tester.assertEqual(task.status, 'ready')

    def wait_error(self, task, timeout=60, message=None):
        self._wait_task(task, timeout, message)
        self.tester.assertEqual(task.status, 'error')

    def wait_for_nodes_status(self, nodes, status):
        def check_statuses():
            self.refresh_nodes()

            nodes_with_status = filter(
                lambda x: x.status in status,
                nodes)

            return len(nodes) == len(nodes_with_status)

        self.wait_for_true(
            check_statuses,
            error_message='Something wrong with the statuses')

    def wait_for_true(self, check, args=[], kwargs={},
                      timeout=60, error_message='Timeout error'):

        start_time = time.time()

        while True:
            result = check(*args, **kwargs)
            if result:
                return result
            if time.time() - start_time > timeout:
                raise TimeoutError(error_message)
            time.sleep(0.1)

    def _api_get(self, method, instance_id, expect_errors=False):
        return self.app.get(
            reverse(method,
                    kwargs=instance_id),
            headers=self.default_headers,
            expect_errors=expect_errors)

    def _api_put(self, method, instance_id, data, expect_errors=False):
        return self.app.put(
            reverse(method,
                    kwargs=instance_id),
            jsonutils.dumps(data),
            headers=self.default_headers,
            expect_errors=expect_errors)

    def nova_networks_get(self, cluster_id, expect_errors=False):
        return self._api_get('NovaNetworkConfigurationHandler',
                             {'cluster_id': cluster_id},
                             expect_errors)

    def nova_networks_put(self, cluster_id, networks, expect_errors=False):
        return self._api_put('NovaNetworkConfigurationHandler',
                             {'cluster_id': cluster_id},
                             networks,
                             expect_errors)

    def neutron_networks_get(self, cluster_id, expect_errors=False):
        return self._api_get('NeutronNetworkConfigurationHandler',
                             {'cluster_id': cluster_id},
                             expect_errors)

    def neutron_networks_put(self, cluster_id, networks, expect_errors=False):
        return self._api_put('NeutronNetworkConfigurationHandler',
                             {'cluster_id': cluster_id},
                             networks,
                             expect_errors)

    def cluster_changes_put(self, cluster_id, expect_errors=False):
        return self._api_put('ClusterChangesHandler',
                             {'cluster_id': cluster_id},
                             [],
                             expect_errors)

    def node_nics_get(self, node_id, expect_errors=False):
        return self._api_get('NodeNICsHandler',
                             {'node_id': node_id},
                             expect_errors)

    def node_nics_put(self, node_id, interfaces, expect_errors=False):
        return self._api_put('NodeNICsHandler',
                             {'node_id': node_id},
                             interfaces,
                             expect_errors)

    def node_collection_nics_put(self, nodes,
                                 expect_errors=False):
        return self._api_put('NodeCollectionNICsHandler',
                             {},
                             nodes,
                             expect_errors)


class BaseTestCase(TestCase):

    fixtures = ['admin_network', 'master_node_settings']

    def __init__(self, *args, **kwargs):
        super(BaseTestCase, self).__init__(*args, **kwargs)
        self.default_headers = {
            "Content-Type": "application/json"
        }

    @classmethod
    def setUpClass(cls):
        cls.app = app.TestApp(
            build_app(db_driver=test_db_driver).wsgifunc()
        )
        syncdb()

    def setUp(self):
        self.db = db
        flush()
        self.env = EnvironmentManager(app=self.app, session=self.db)
        self.env.upload_fixtures(self.fixtures)

    def tearDown(self):
        self.db.remove()

    def assertNotRaises(self, exception, method, *args, **kwargs):
        try:
            method(*args, **kwargs)
        except exception:
            self.fail('Exception "{0}" raised.'.format(exception))

    def datadiff(self, node1, node2, path=None, ignore_keys=[],
                 compare_sorted=False):
        if path is None:
            path = []

        def fail(msg, failed_path):
            self.fail('Path "{0}": {1}'.format("->".join(failed_path), msg))

        if not isinstance(node1, dict) or not isinstance(node2, dict):
            if isinstance(node1, (list, tuple)):
                newpath = path[:]
                if compare_sorted:
                    node1 = sorted(node1)
                    node2 = sorted(node2)
                for i, keys in enumerate(izip(node1, node2)):
                    newpath.append(str(i))
                    self.datadiff(keys[0], keys[1], newpath, ignore_keys,
                                  compare_sorted)
                    newpath.pop()
            elif node1 != node2:
                err = "Values differ: {0} != {1}".format(
                    str(node1),
                    str(node2)
                )
                fail(err, path)
        else:
            newpath = path[:]

            if len(node1) != len(node2):
                fail('Nodes have different keys number: {0} != {1}'.format(
                    len(node1), len(node2)), path)

            for key1, key2 in zip(
                sorted(node1),
                sorted(node2)
            ):
                if key1 != key2:
                    err = "Keys differ: {0} != {1}".format(
                        str(key1),
                        str(key2)
                    )
                    fail(err, path)
                if key1 in ignore_keys:
                    continue
                newpath.append(key1)
                self.datadiff(node1[key1], node2[key2], newpath, ignore_keys,
                              compare_sorted)
                newpath.pop()


class BaseIntegrationTest(BaseTestCase):
    @classmethod
    def setUpClass(cls):
        super(BaseIntegrationTest, cls).setUpClass()
        nailgun.task.task.DeploymentTask._prepare_syslog_dir = mock.Mock()

    def _wait_for_threads(self):
        # wait for fake task thread termination
        import threading
        for thread in threading.enumerate():
            if thread is not threading.currentThread():
                if hasattr(thread, "rude_join"):
                    timer = time.time()
                    timeout = 25
                    thread.rude_join(timeout)
                    if time.time() - timer > timeout:
                        raise Exception(
                            '{0} seconds is not enough'
                            ' - possible hanging'.format(
                                timeout
                            )
                        )


class BaseAuthenticationIntegrationTest(BaseIntegrationTest):
    @classmethod
    def setUpClass(cls):
        cls.app = app.TestApp(build_app(db_driver=test_db_driver).wsgifunc(
            NailgunFakeKeystoneAuthMiddleware))
        syncdb()
        nailgun.task.task.DeploymentTask._prepare_syslog_dir = mock.Mock()


class BaseUnitTest(TestCase):
    pass


def fake_tasks(fake_rpc=True,
               mock_rpc=True,
               tick_count=100,
               tick_interval=0,
               **kwargs):
    def wrapper(func):
        func = mock.patch(
            'nailgun.task.task.settings.FAKE_TASKS',
            True
        )(func)
        func = mock.patch(
            'nailgun.task.fake.settings.FAKE_TASKS_TICK_COUNT',
            tick_count
        )(func)
        func = mock.patch(
            'nailgun.task.fake.settings.FAKE_TASKS_TICK_INTERVAL',
            tick_interval
        )(func)
        if fake_rpc:
            func = mock.patch(
                'nailgun.task.task.rpc.cast',
                partial(
                    nailgun.task.task.fake_cast,
                    **kwargs
                )
            )(func)
        elif mock_rpc:
            func = mock.patch(
                'nailgun.task.task.rpc.cast',
                **kwargs
            )(func)
        return func
    return wrapper


def reverse(name, kwargs=None):
    urldict = dict(zip(urls[1::2], urls[::2]))
    url = urldict[name]
    urlregex = re.compile(url)
    for kwarg in urlregex.groupindex:
        if kwarg not in kwargs:
            raise KeyError("Invalid argument specified")
        url = re.sub(
            r"\(\?P<{0}>[^)]+\)".format(kwarg),
            str(kwargs[kwarg]),
            url,
            1
        )
    url = re.sub(r"\??\$", "", url)
    return "/api" + url


# this method is for development and troubleshooting purposes
def datadiff(data1, data2, branch, p=True):
    def iterator(data1, data2):
        if isinstance(data1, (list,)) and isinstance(data2, (list,)):
            return xrange(max(len(data1), len(data2)))
        elif isinstance(data1, (dict,)) and isinstance(data2, (dict,)):
            return (set(data1.keys()) | set(data2.keys()))
        else:
            raise TypeError

    diff = []
    if data1 != data2:
        try:
            it = iterator(data1, data2)
        except Exception:
            return [(branch, data1, data2)]

        for k in it:
            newbranch = branch[:]
            newbranch.append(k)

            if p:
                print("Comparing branch: %s" % newbranch)
            try:
                try:
                    v1 = data1[k]
                except (KeyError, IndexError):
                    if p:
                        print("data1 seems does not have key = %s" % k)
                    diff.append((newbranch, None, data2[k]))
                    continue
                try:
                    v2 = data2[k]
                except (KeyError, IndexError):
                    if p:
                        print("data2 seems does not have key = %s" % k)
                    diff.append((newbranch, data1[k], None))
                    continue

            except Exception:
                if p:
                    print("data1 and data2 cannot be compared on "
                          "branch: %s" % newbranch)
                return diff.append((newbranch, data1, data2))

            else:
                if v1 != v2:
                    if p:
                        print("data1 and data2 do not match "
                              "each other on branch: %s" % newbranch)
                        # print("data1 = %s" % data1)
                        print("v1 = %s" % v1)
                        # print("data2 = %s" % data2)
                        print("v2 = %s" % v2)
                    diff.extend(datadiff(v1, v2, newbranch))
    return diff


@six.add_metaclass(abc.ABCMeta)
class BaseAlembicMigrationTest(TestCase):

    @classmethod
    def setUpClass(cls):
        super(BaseAlembicMigrationTest, cls).setUpClass()

        dropdb()
        alembic.command.upgrade(ALEMBIC_CONFIG, cls.prepare_revision)
        cls.prepare()
        alembic.command.upgrade(ALEMBIC_CONFIG, cls.test_revision)

    def tearDown(self):
        db.remove()
        super(BaseAlembicMigrationTest, self).tearDown()

    @abc.abstractproperty
    def prepare_revision(self):
        """Alembic revision to stop and call prepare() method."""

    @abc.abstractproperty
    def test_revision(self):
        """Alembic revision to stop and call text() method."""

    @classmethod
    @abc.abstractmethod
    def prepare(cls):
        """Method that should prepare your database state."""
