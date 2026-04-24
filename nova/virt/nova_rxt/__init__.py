# Copyright 2026 Cloudnull <kevin@cloudnull.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# NOTE(cloudnull): Intentionally no import-time side effects. The driver's inertness
#                  contract requires that loading this package on a compute host does
#                  not alter stock LibvirtDriver behavior. In particular, we do NOT
#                  patch nova.virt.libvirt.blockinfo.SUPPORTED_DEVICE_BUSES or
#                  nova.virt.libvirt.vif.SUPPORTED_VIF_MODELS at import time, because
#                  those dicts feed both validation (is_disk_bus_valid_for_virt) and
#                  placement trait reporting (_get_vif_model_traits /
#                  _get_storage_bus_traits) for every guest the driver renders, not
#                  just Xen-tagged ones.
