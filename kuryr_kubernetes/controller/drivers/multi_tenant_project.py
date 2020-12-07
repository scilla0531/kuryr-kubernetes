# Copyright (c) 2016 Mirantis, Inc.
# All Rights Reserved.
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

from oslo_config import cfg

from kuryr_kubernetes import config
from kuryr_kubernetes.controller.drivers import base
from kuryr_kubernetes.controller.drivers import utils as driver_utils


class MultiTenantPodProjectDriver(base.PodProjectDriver):
    """Get the projectId from the kns.spec.projectId"""

    def get_project(self, pod):
        namespace = pod['metadata']['namespace']
        net_crd = driver_utils.get_kuryrnetwork_crds(namespace)
        return net_crd['spec'].get('projectId')


class MultiTenantServiceProjectDriver(base.ServiceProjectDriver):

    def get_project(self, service):
        namespace = service['metadata']['namespace']
        net_crd = driver_utils.get_kuryrnetwork_crds(namespace)
        return net_crd['spec'].get('projectId')


class MultiTenantNetworkPolicyProjectDriver(base.NetworkPolicyProjectDriver):

    def get_project(self, policy):
        namespace = policy['metadata']['namespace']
        net_crd = driver_utils.get_kuryrnetwork_crds(namespace)
        return net_crd['spec'].get('projectId')
