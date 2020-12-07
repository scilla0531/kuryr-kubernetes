# Copyright 2020 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from oslo_config import cfg as oslo_cfg
from oslo_log import log as logging

from kuryr_kubernetes import clients
from kuryr_kubernetes import constants
from kuryr_kubernetes.controller.drivers import base as drivers
from kuryr_kubernetes.controller.drivers import utils as driver_utils
from kuryr_kubernetes import exceptions as k_exc
from kuryr_kubernetes.handlers import k8s_base

LOG = logging.getLogger(__name__)


class KuryrNetworkHandler(k8s_base.ResourceEventHandler):
    """Controller side of KuryrNetwork process for Kubernetes pods.

    `KuryrNetworkHandler` runs on the Kuryr-Kubernetes controller and is
    responsible for creating the OpenStack resources associated to the
    newly created namespaces, and update the KuryrNetwork CRD status with
    them.
    """
    OBJECT_KIND = constants.K8S_OBJ_KURYRNETWORK
    OBJECT_WATCH_PATH = constants.K8S_API_CRD_KURYRNETWORKS

    def __init__(self):
        super(KuryrNetworkHandler, self).__init__()
        self._drv_project = drivers.NamespaceProjectDriver.get_instance()
        self._drv_subnets = drivers.PodSubnetsDriver.get_instance()
        self._drv_sg = drivers.PodSecurityGroupsDriver.get_instance()
        self._drv_vif_pool = drivers.VIFPoolDriver.get_instance(
            specific_driver='multi_pool')
        self._drv_vif_pool.set_vif_driver()
        if self._is_network_policy_enabled():
            self._drv_lbaas = drivers.LBaaSDriver.get_instance()
            self._drv_svc_sg = (
                drivers.ServiceSecurityGroupsDriver.get_instance())

    def on_present(self, kuryrnet_crd):
        ns_name = kuryrnet_crd['spec']['nsName']
        project_id = kuryrnet_crd['spec']['projectId']
        kns_status = kuryrnet_crd.get('status', {})

        crd_creation = False
        net_id = kns_status.get('netId')
        if not net_id:
            net_id = self._drv_subnets.create_network(ns_name, project_id)
            status = {'netId': net_id}
            self._patch_kuryrnetwork_crd(kuryrnet_crd, status)
            crd_creation = True
        subnet_id = kns_status.get('subnetId')
        if not subnet_id or crd_creation:
            subnet_id, subnet_cidr = self._drv_subnets.create_subnet(
                ns_name, project_id, net_id)
            status = {'subnetId': subnet_id, 'subnetCIDR': subnet_cidr}
            self._patch_kuryrnetwork_crd(kuryrnet_crd, status)
            crd_creation = True
        if not kns_status.get('routerId') or crd_creation:
            router_id = self._drv_subnets.add_subnet_to_router(subnet_id)
            status = {'routerId': router_id, 'populated': False}
            self._patch_kuryrnetwork_crd(kuryrnet_crd, status)
            crd_creation = True

        # check labels to create sg rules
        ns_labels = kns_status.get('nsLabels', {})
        if (crd_creation or
                ns_labels != kuryrnet_crd['spec']['nsLabels']):
            # update SG and svc SGs
            namespace = driver_utils.get_namespace(ns_name)
            crd_selectors = self._drv_sg.update_namespace_sg_rules(namespace)
            if (self._is_network_policy_enabled() and crd_selectors and
                    oslo_cfg.CONF.octavia_defaults.enforce_sg_rules):
                services = driver_utils.get_services()
                self._update_services(services, crd_selectors, project_id)
            # update status
            status = {'nsLabels': kuryrnet_crd['spec']['nsLabels']}
            self._patch_kuryrnetwork_crd(kuryrnet_crd, status, labels=True)

    def on_finalize(self, kuryrnet_crd):
        LOG.debug("Deleting kuryrnetwork CRD resources: %s", kuryrnet_crd)

        net_id = kuryrnet_crd.get('status', {}).get('netId')
        if net_id:
            self._drv_vif_pool.delete_network_pools(
                kuryrnet_crd['status']['netId'])
            if not kuryrnet_crd['spec'].get('is_tenant'):
                try:
                    self._drv_subnets.delete_namespace_subnet(kuryrnet_crd)
                except k_exc.ResourceNotReady:
                    LOG.debug("Subnet is not ready to be removed.")
                    # TODO(ltomasbo): Once KuryrPort CRDs is supported,
                    # we should execute a delete network ports method here to
                    # remove the ports associated to the namespace/subnet,
                    # ensuring next retry will be successful
                    raise

            namespace = {
                'metadata': {'name': kuryrnet_crd['spec']['nsName']}}
            crd_selectors = self._drv_sg.delete_namespace_sg_rules(namespace)

        if (self._is_network_policy_enabled() and crd_selectors and
                oslo_cfg.CONF.octavia_defaults.enforce_sg_rules):
            project_id = kuryrnet_crd['spec']['projectId']
            services = driver_utils.get_services()
            self._update_services(services, crd_selectors, project_id)

        kubernetes = clients.get_kubernetes_client()
        LOG.debug('Removing finalizer for KuryrNet CRD %s', kuryrnet_crd)
        try:
            kubernetes.remove_finalizer(kuryrnet_crd,
                                        constants.KURYRNETWORK_FINALIZER)
        except k_exc.K8sClientException:
            LOG.exception('Error removing kuryrnetwork CRD finalizer for %s',
                          kuryrnet_crd)
            raise

    def _is_network_policy_enabled(self):
        enabled_handlers = oslo_cfg.CONF.kubernetes.enabled_handlers
        svc_sg_driver = oslo_cfg.CONF.kubernetes.service_security_groups_driver
        return ('policy' in enabled_handlers and svc_sg_driver == 'policy')

    def _update_services(self, services, crd_selectors, project_id):
        for service in services.get('items'):
            if not driver_utils.service_matches_affected_pods(
                    service, crd_selectors):
                continue
            sgs = self._drv_svc_sg.get_security_groups(service,
                                                       project_id)
            self._drv_lbaas.update_lbaas_sg(service, sgs)

    def _patch_kuryrnetwork_crd(self, kuryrnet_crd, status, labels=False):
        kubernetes = clients.get_kubernetes_client()
        LOG.debug('Patching KuryrNetwork CRD %s', kuryrnet_crd)
        try:
            if labels:
                kubernetes.patch_crd('status',
                                     kuryrnet_crd['metadata']['selfLink'],
                                     status)
            else:
                kubernetes.patch('status',
                                 kuryrnet_crd['metadata']['selfLink'],
                                 status)
        except k_exc.K8sResourceNotFound:
            LOG.debug('KuryrNetwork CRD not found %s', kuryrnet_crd)
        except k_exc.K8sClientException:
            LOG.exception('Error updating kuryrNetwork CRD %s', kuryrnet_crd)
            raise
