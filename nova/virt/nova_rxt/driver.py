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

"""LibvirtDriver subclass enabling QEMU 8.1+ Xen emulation for PVHVM guests.

See plan/nova-xen-driver.md for the inertness contract: the driver is a
byte-for-byte passthrough over stock :class:`LibvirtDriver` unless an
image carries ``ospc:hw_hypervisor_interface=xen``.
"""

import collections
import fcntl
import os
import struct
import threading

from oslo_config import cfg
from oslo_log import log as logging

from nova import exception
from nova.virt.libvirt import driver as libvirt_driver

from nova.virt.nova_rxt import xml as xmlinject


LOG = logging.getLogger(__name__)


# NOTE(cloudnull): Opt-in key and value. The ``ospc:`` prefix is a plugin-owned namespace so
#                  the property cannot collide with any current or future upstream
#                  Nova/Glance key and is not mistaken for a standard ``img_*`` / ``hw_*``
#                  property when an image is moved between clouds.
_XEN_OPTIN_KEY = "ospc:hw_hypervisor_interface"
_XEN_OPTIN_VALUE = "xen"
_SYSMETA_PREFIX = "image_"


# NOTE(cloudnull): KVM ioctl constants for the host capability probe.
#                  KVM_CREATE_VM      = _IO (KVMIO=0xAE, 0x01) → 0xAE01
#                  KVM_XEN_HVM_CONFIG = _IOW(KVMIO=0xAE, 0x7a, struct
#                                            kvm_xen_hvm_config /* 56 bytes */)
#                  which encodes to (_IOC_WRITE<<30) | (56<<16) |
#                  (KVMIO<<8) | nr = 0x4038AE7A.
KVMIO = 0xAE
KVM_CREATE_VM = 0xAE01
_KVM_XEN_HVM_CONFIG_SIZE = 56
KVM_XEN_HVM_CONFIG = (1 << 30) | (_KVM_XEN_HVM_CONFIG_SIZE << 16) | (KVMIO << 8) | 0x7A
KVM_XEN_HVM_CONFIG_INTERCEPT_HCALL = 1 << 1
# NOTE(cloudnull): Xen's conventional hypercall MSR. Any non-zero value
#                  satisfies the kernel's requirement that
#                  INTERCEPT_HCALL be paired with a non-zero msr; the
#                  scratch VM we write it to is discarded immediately
#                  so nothing persists on the host.
_XEN_HYPERCALL_MSR = 0x40000000


# NOTE(cloudnull): Optional placement trait. Not part of ``os-traits`` (yet), so the name
#                  carries the mandatory ``CUSTOM_`` prefix. Published only when both the
#                  operator has explicitly opted in via
#                  ``[xen_emulation] publish_trait`` *and* the host-level capability
#                  probe succeeded. Default OFF so installing the driver is a no-op for
#                  placement.
TRAIT_XEN_EMULATION = "CUSTOM_COMPUTE_XEN_EMULATION"


# NOTE(cloudnull): Per-instance LRU cache for the (untagged-image) Glance fallback.
#                  The fallback only fires when sysmeta lacks the opt-in key. For typical
#                  compute workloads this cache spares a second glance.get() inside a
#                  single spawn flow (once from _get_guest_config, once from _get_guest_xml).
_OPT_IN_CACHE_LIMIT = 256
_opt_in_cache = collections.OrderedDict()
_opt_in_cache_lock = threading.Lock()


# NOTE(cloudnull): Config: the exact QEMU argument tuple emitted under <qemu:commandline>.
#                  Exposed as a CONF option so operators can re-tune the Xen ABI version
#                  without a code release (e.g. if future guest kernels probe for a newer
#                  ABI than 0x40011 / Xen 4.10).
xen_emulation_opts = [
    cfg.ListOpt(
        "qemu_args",
        default=[
            "-accel",
            "kvm,xen-version=0x40011,kernel-irqchip=split",
        ],
        help=(
            "QEMU command-line arguments injected under <qemu:commandline> "
            "for instances tagged with ospc:hw_hypervisor_interface=xen. "
            "The default targets the Xen 4.10 ABI, which is what common "
            "PVHVM guest kernels probe for. ``kernel-irqchip=split`` is "
            "mandatory: QEMU's Xen emulation refuses to initialise with "
            "the default in-kernel irqchip because Xen-style event "
            "channel upcalls are delivered via the userspace IOAPIC path. "
            "For KVM Xen emulation the guest discovers Xen via CPUID "
            "leaf 0x40000000 and the hypercall MSR set up by "
            "``-accel``; no PCI platform device is required."
        ),
    ),
    cfg.BoolOpt(
        "publish_trait",
        default=True,
        help=(
            "When True, publish the %(trait)s placement trait on compute "
            "nodes where the Xen capability probe succeeded, so operators "
            "can select Xen-capable hosts with "
            "``--property trait:%(trait)s=required`` on images or flavors. "
            "Default True — the trait is still gated on the host-level "
            "capability probe, so non-Xen-capable hosts never advertise "
            "it and the trait has zero scheduling effect until some "
            "image or flavor asks for it. Set False to keep Placement "
            "byte-for-byte identical to stock, e.g. when you prefer "
            "host aggregates or want to verify inertness during a "
            "controlled rollout."
        )
        % {"trait": TRAIT_XEN_EMULATION},
    ),
    cfg.StrOpt(
        "kvm_xen_probe",
        default="auto",
        choices=["auto", "assume_capable", "assume_uncapable"],
        help=(
            "How to decide whether this host's KVM can run QEMU's Xen "
            "emulation. ``auto`` (default) opens /dev/kvm and drives "
            "the ``KVM_XEN_HVM_CONFIG`` ioctl — correct for bare-metal "
            "or privileged-container deployments. Set to "
            "``assume_capable`` in Kubernetes deployments where "
            "nova-compute runs in a pod that cannot open /dev/kvm but "
            "libvirtd on the host can: the real capability lives with "
            "libvirtd, the pod-side probe is asking the wrong kernel, "
            "and spawn-time still raises ``InternalError`` if a "
            "Xen-tagged build genuinely fails on this host. Set to "
            "``assume_uncapable`` to force the trait off (staged "
            "rollout, or to hide a partially-capable host from the "
            "scheduler) without touching ``publish_trait``."
        ),
    ),
]

CONF = cfg.CONF
CONF.register_opts(xen_emulation_opts, group="xen_emulation")


def _get_prop(props, name, default=None):
    """Read a possibly-unset field on an ImageMetaProps versioned object."""
    try:
        obj_attr_is_set = getattr(props, "obj_attr_is_set", None)
        if obj_attr_is_set is not None and not obj_attr_is_set(name):
            return default
        return getattr(props, name, default)
    except (AttributeError, NotImplementedError):
        return default


def _fetch_image_opt_in(context, instance):
    """Raw Glance fetch of the opt-in key. Split out so tests can mock it."""
    try:
        # Lazy-import to keep unit tests loadable when nova isn't installed
        # and to avoid pulling in nova.image.glance at package-import time.
        from nova.image import glance

        img = glance.API().get(context, instance.image_ref)
        return (img.get("properties") or {}).get(_XEN_OPTIN_KEY)
    except Exception as e:
        LOG.debug(
            "Glance opt-in lookup for instance %s failed: %s",
            getattr(instance, "uuid", "?"),
            e,
            exc_info=True,
        )
        return None


def _cached_glance_opt_in(context, instance):
    uuid = getattr(instance, "uuid", None)
    if uuid is not None:
        with _opt_in_cache_lock:
            if uuid in _opt_in_cache:
                _opt_in_cache.move_to_end(uuid)
                return _opt_in_cache[uuid]
    val = _fetch_image_opt_in(context, instance)
    if uuid is not None:
        with _opt_in_cache_lock:
            _opt_in_cache[uuid] = val
            while len(_opt_in_cache) > _OPT_IN_CACHE_LIMIT:
                _opt_in_cache.popitem(last=False)
    return val


def _xen_requested(context, instance):
    """Return True iff the instance's image opts into Xen emulation.

    Read order:
      1. ``instance.system_metadata['image_ospc:hw_hypervisor_interface']``
         — Nova copies raw Glance properties into system_metadata with an
         ``image_`` prefix; this is the authoritative bind-time value.
      2. Glance fallback (cached) — in case the key was absent from
         sysmeta for any reason (e.g. instance predates the plugin).
    """
    sysmeta = getattr(instance, "system_metadata", None) or {}
    val = sysmeta.get(_SYSMETA_PREFIX + _XEN_OPTIN_KEY)
    if val is None:
        val = _cached_glance_opt_in(context, instance)
    return val == _XEN_OPTIN_VALUE


def _kvm_xen_hvm_capable():
    """Return True iff KVM on this host can run QEMU's Xen emulation.

    Mirrors ``target/i386/kvm/xen-emu.c:kvm_xen_init`` in QEMU: open
    /dev/kvm, create a scratch VM, and attempt
    ``KVM_XEN_HVM_CONFIG`` with the ``INTERCEPT_HCALL`` flag. If the
    kernel accepts it, QEMU's Xen-emu init will also succeed on this
    host; if it returns ``-ENOTTY`` / ``-EINVAL``, QEMU would refuse
    with "Xen HVM guest support not present or insufficient".

    Why not ``KVM_CHECK_EXTENSION(KVM_CAP_XEN_HVM)``: some kernels
    (notably certain downstream-patched builds) under-report the
    returned bitmap even when the feature is fully present, and some
    conversely advertise ``HYPERCALL_MSR`` from a legacy 5.6-era stub
    path when nothing else works. Driving the actual config ioctl is
    the only check that reliably agrees with what QEMU will do at
    guest-start time.

    The scratch VM is torn down immediately; the config ioctl on it
    has no lasting effect on the host.
    """
    try:
        dev_fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
    except OSError as e:
        LOG.debug("/dev/kvm open failed: %s", e)
        return False
    vm_fd = -1
    try:
        try:
            vm_fd = fcntl.ioctl(dev_fd, KVM_CREATE_VM, 0)
        except (OSError, IOError) as e:
            LOG.debug("KVM_CREATE_VM failed: %s", e)
            return False
        # struct kvm_xen_hvm_config {
        #     u32 flags; u32 msr;
        #     u64 blob_addr_32; u64 blob_addr_64;
        #     u8  blob_size_32; u8  blob_size_64;
        #     u8  pad2[30];
        # }  — 56 bytes, naturally aligned on x86_64.
        cfg = struct.pack(
            "=IIQQBB30x",
            KVM_XEN_HVM_CONFIG_INTERCEPT_HCALL,  # flags
            _XEN_HYPERCALL_MSR,  # msr
            0,
            0,  # blob_addr_32, blob_addr_64
            0,
            0,  # blob_size_32, blob_size_64
        )
        try:
            fcntl.ioctl(vm_fd, KVM_XEN_HVM_CONFIG, cfg)
        except (OSError, IOError) as e:
            LOG.debug("KVM_XEN_HVM_CONFIG(INTERCEPT_HCALL) failed: %s", e)
            return False
        return True
    finally:
        if vm_fd != -1:
            try:
                os.close(vm_fd)
            except OSError:
                pass
        try:
            os.close(dev_fd)
        except OSError:
            pass


class XenEmulationDriver(libvirt_driver.LibvirtDriver):
    """Optional LibvirtDriver subclass enabling QEMU 8.1+ Xen emulation.

    Activated per-instance via the Glance image property
    ``ospc:hw_hypervisor_interface=xen``. All other guests are rendered
    exactly as stock :class:`LibvirtDriver` would render them.
    """

    def __init__(self, virtapi, read_only=False):
        super().__init__(virtapi, read_only=read_only)
        # Populated by init_host(); False until the host capability probe
        # succeeds. Consulted at spawn time so a Xen-tagged build on an
        # uncapable host fails loudly while all other builds continue.
        self._xen_capable = False

    def init_host(self, host):
        super().init_host(host)
        qemu_ok = self._probe_qemu_xen_version()
        kvm_ok, kvm_source = self._resolve_kvm_xen_capable()
        self._xen_capable = bool(qemu_ok and kvm_ok)
        if self._xen_capable:
            LOG.info(
                "QEMU Xen emulation available on this host " "(KVM capability: %s)",
                kvm_source,
            )
        else:
            # Soft failure only — we must never break normal KVM workloads
            # on this host just because the Xen emulator is missing.
            LOG.warning(
                "Xen emulation capability probe failed "
                "(QEMU>=8.1.0: %(qemu)s, KVM_CAP_XEN_HVM: %(kvm)s "
                "[%(source)s]). Normal KVM guests continue to function; "
                "only ospc:hw_hypervisor_interface=xen builds will be "
                "refused.",
                {"qemu": qemu_ok, "kvm": kvm_ok, "source": kvm_source},
            )

    def _resolve_kvm_xen_capable(self):
        """Honor ``[xen_emulation] kvm_xen_probe``; fall back to probing.

        Returns ``(capable, source_description)``. The source string
        is threaded into log lines so operators can tell the difference
        between a host that actually passed the ioctl probe and one
        where the probe was bypassed by configuration.
        """
        mode = CONF.xen_emulation.kvm_xen_probe
        if mode == "assume_capable":
            return True, "assume_capable (probe skipped)"
        if mode == "assume_uncapable":
            return False, "assume_uncapable (probe skipped)"
        return _kvm_xen_hvm_capable(), "probed"

    def _probe_qemu_xen_version(self):
        try:
            return bool(self._host.has_min_version(hv_ver=(8, 1, 0)))
        except Exception:
            LOG.debug("QEMU min-version probe raised", exc_info=True)
            return False

    # NOTE(cloudnull): Signature tracks Nova 2025.1's ``_get_guest_config``
    #                  up through ``context`` — the last param we read
    #                  locally — then falls through to ``*args/**kwargs``
    #                  so any extra trailing parameters a future Nova
    #                  adds (share_info is already in 2024.2+, future
    #                  releases may add more) are forwarded to super()
    #                  without a signature match on our side. Naming
    #                  share_info / mdevs / accel_info here would lock
    #                  us to one Nova release and break spawn on the
    #                  others.
    def _get_guest_config(
        self,
        instance,
        network_info,
        image_meta,
        disk_info,
        rescue=None,
        block_device_info=None,
        context=None,
        *args,
        **kwargs,
    ):
        if not _xen_requested(context, instance):
            # Inert path: byte-for-byte identical to stock LibvirtDriver.
            return super()._get_guest_config(
                instance,
                network_info,
                image_meta,
                disk_info,
                rescue,
                block_device_info,
                context,
                *args,
                **kwargs,
            )

        if not self._xen_capable:
            raise exception.InternalError(
                "ospc:hw_hypervisor_interface=xen requested but this host "
                "lacks QEMU >= 8.1.0 or KVM_CAP_XEN_HVM"
            )

        props = image_meta.properties
        vm_mode = _get_prop(props, "hw_vm_mode")
        if vm_mode and vm_mode != "hvm":
            raise exception.InvalidMetadata(
                reason=(
                    "Xen emulation PVHVM requires hw_vm_mode=hvm (or unset); "
                    "got %s" % vm_mode
                )
            )

        # No bus/device renaming: libvirt's QEMU driver hard-rejects
        # bus='xen' on a kvm domain (qemu_command.c:qemuBuildDiskDeviceProps
        # has VIR_DOMAIN_DISK_BUS_XEN in its rejection arm), so trying to
        # emit Xen PV disks/NICs through the standard <disk>/<interface>
        # path is unbuildable. Instead the driver only injects the
        # -accel kvm,xen-version=...,kernel-irqchip=split args (in
        # _get_guest_xml below) — that gives the guest a Xen-shaped
        # CPU/hypervisor interface (CPUID leaf 0x40000000, hypercall MSR,
        # shared_info, event channels, runstate). I/O stays virtio. The
        # captured image is expected to carry virtio-blk/-net drivers in
        # its initramfs (the migration script handles that on the
        # source side).
        return super()._get_guest_config(
            instance,
            network_info,
            image_meta,
            disk_info,
            rescue,
            block_device_info,
            context,
            *args,
            **kwargs,
        )

    def update_provider_tree(self, provider_tree, nodename, allocations=None, **kwargs):
        super().update_provider_tree(provider_tree, nodename, allocations, **kwargs)
        # Gate 1: operator must explicitly opt in. Off by default so
        # installing the driver is a no-op for placement.
        if not CONF.xen_emulation.publish_trait:
            return
        # Gate 2: never lie about capability. An operator who flips the
        # opt-in still only gets the trait advertised on hosts where
        # the probe actually passed.
        if not self._xen_capable:
            LOG.debug(
                "xen_emulation.publish_trait=True but host is not "
                "Xen-capable; %s will NOT be published on %s.",
                TRAIT_XEN_EMULATION,
                nodename,
            )
            return
        provider_tree.add_traits(nodename, TRAIT_XEN_EMULATION)

    # NOTE(cloudnull): Same rationale as _get_guest_config — keep only
    #                  the params we actually read (context, instance)
    #                  and forward the rest. We don't need to know
    #                  whether Nova's signature has 8 positional slots
    #                  or 12; ``*args, **kwargs`` matches whatever it
    #                  passes us.
    def _get_guest_xml(self, context, instance, *args, **kwargs):
        xml = super()._get_guest_xml(context, instance, *args, **kwargs)
        if not _xen_requested(context, instance):
            return xml
        return xmlinject.inject_qemu_commandline(
            xml, list(CONF.xen_emulation.qemu_args)
        )
