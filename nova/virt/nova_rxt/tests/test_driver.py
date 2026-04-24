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

"""Unit tests for XenEmulationDriver.

Nova itself is a large dependency. These tests stub the minimal
subset of ``nova.*`` modules the driver imports so the test suite runs
standalone (tox -e py3) without a full Nova install. Where a real Nova
is already on sys.path, the stubs defer to it.
"""

import inspect
import sys
import types
import unittest
from unittest import mock


# ---------------------------------------------------------------------------
# Install minimal nova.* stubs before importing the driver.
# ---------------------------------------------------------------------------


def _install_fake_nova():
    if (
        "nova" in sys.modules
        and getattr(sys.modules["nova"], "__fake_xen_emulation_stub__", False) is False
        and hasattr(sys.modules["nova"], "exception")
    ):
        return  # A real nova is already imported; don't shadow it.

    nova = types.ModuleType("nova")
    nova.__fake_xen_emulation_stub__ = True

    nova_exception = types.ModuleType("nova.exception")

    class _NovaException(Exception):
        def __init__(self, message=None, **kwargs):
            super().__init__(message or "")
            self.kwargs = kwargs

    class InternalError(_NovaException):
        pass

    class InvalidMetadata(_NovaException):
        pass

    nova_exception.InternalError = InternalError
    nova_exception.InvalidMetadata = InvalidMetadata

    nova_image = types.ModuleType("nova.image")
    nova_image_glance = types.ModuleType("nova.image.glance")

    class _GlanceAPI:
        def get(self, context, image_ref):
            return {"properties": {}}

    nova_image_glance.API = _GlanceAPI

    nova_virt = types.ModuleType("nova.virt")
    nova_virt_libvirt = types.ModuleType("nova.virt.libvirt")
    nova_virt_libvirt_driver = types.ModuleType("nova.virt.libvirt.driver")
    nova_virt_libvirt_config = types.ModuleType("nova.virt.libvirt.config")

    class LibvirtDriver:
        def __init__(self, virtapi=None, read_only=False):
            self.virtapi = virtapi
            self.read_only = read_only
            self._host = mock.MagicMock()

        def init_host(self, host):
            return None

        def _get_guest_config(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError("stub — patch in tests")

        def _get_guest_xml(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError("stub — patch in tests")

        def update_provider_tree(self, *args, **kwargs):  # pragma: no cover
            raise NotImplementedError("stub — patch in tests")

        def _get_vif_model_traits(self):  # pragma: no cover
            return []

        def _get_storage_bus_traits(self):  # pragma: no cover
            return []

    nova_virt_libvirt_driver.LibvirtDriver = LibvirtDriver

    class LibvirtConfigGuestDisk:
        def __init__(self):
            self.target_bus = None
            self.target_dev = None

    class LibvirtConfigGuestInterface:
        def __init__(self):
            self.model = None

    nova_virt_libvirt_config.LibvirtConfigGuestDisk = LibvirtConfigGuestDisk
    nova_virt_libvirt_config.LibvirtConfigGuestInterface = LibvirtConfigGuestInterface

    # Register in sys.modules and link attributes for dotted access.
    for name, mod in [
        ("nova", nova),
        ("nova.exception", nova_exception),
        ("nova.image", nova_image),
        ("nova.image.glance", nova_image_glance),
        ("nova.virt", nova_virt),
        ("nova.virt.libvirt", nova_virt_libvirt),
        ("nova.virt.libvirt.driver", nova_virt_libvirt_driver),
        ("nova.virt.libvirt.config", nova_virt_libvirt_config),
    ]:
        sys.modules[name] = mod
    nova.exception = nova_exception
    nova.image = nova_image
    nova_image.glance = nova_image_glance
    nova.virt = nova_virt
    nova_virt.libvirt = nova_virt_libvirt
    nova_virt_libvirt.driver = nova_virt_libvirt_driver
    nova_virt_libvirt.config = nova_virt_libvirt_config


_install_fake_nova()

from nova.virt.nova_rxt import driver as xen_driver  # noqa: E402
from nova.virt.nova_rxt import xml as xmlinject  # noqa: E402
from nova.virt.libvirt import config as vconfig  # noqa: E402
from nova.virt.libvirt import driver as libvirt_driver  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


class _FakeImageMetaProps:
    """Minimal stand-in for ``nova.objects.ImageMetaProps``.

    Mirrors the two methods the driver actually touches:
      - ``obj_attr_is_set(name)``
      - attribute read / assignment for known field names.
    """

    def __init__(self, **kwargs):
        object.__setattr__(self, "_fields", dict(kwargs))

    def obj_attr_is_set(self, name):
        return name in self._fields

    def __getattr__(self, name):
        fields = object.__getattribute__(self, "_fields")
        if name in fields:
            return fields[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        self._fields[name] = value


def _make_image_meta(**props):
    meta = mock.MagicMock()
    meta.properties = _FakeImageMetaProps(**props)
    return meta


def _make_instance(sysmeta=None, uuid="instance-uuid-1", image_ref="img-ref-1"):
    inst = mock.MagicMock()
    inst.uuid = uuid
    inst.image_ref = image_ref
    inst.system_metadata = dict(sysmeta or {})
    return inst


def _make_driver(xen_capable=True):
    d = xen_driver.XenEmulationDriver.__new__(xen_driver.XenEmulationDriver)
    d._xen_capable = xen_capable
    d._host = mock.MagicMock()
    return d


def _make_disk_info():
    return {"mapping": {}}


# ---------------------------------------------------------------------------
# Opt-in read path tests.
# ---------------------------------------------------------------------------


class OptInResolutionTest(unittest.TestCase):

    def setUp(self):
        xen_driver._opt_in_cache.clear()

    def test_opt_in_read_from_system_metadata(self):
        instance = _make_instance(
            sysmeta={
                "image_ospc:hw_hypervisor_interface": "xen",
            }
        )
        with mock.patch.object(
            xen_driver,
            "_fetch_image_opt_in",
            return_value=None,
        ) as fake_glance:
            self.assertTrue(xen_driver._xen_requested(mock.Mock(), instance))
        fake_glance.assert_not_called()

    def test_opt_in_fallback_read_from_glance(self):
        instance = _make_instance(sysmeta={})
        with mock.patch.object(
            xen_driver,
            "_fetch_image_opt_in",
            return_value="xen",
        ) as fake_glance:
            self.assertTrue(xen_driver._xen_requested(mock.Mock(), instance))
        fake_glance.assert_called_once()

    def test_glance_fallback_cached_within_process(self):
        instance = _make_instance(sysmeta={})
        with mock.patch.object(
            xen_driver,
            "_fetch_image_opt_in",
            return_value="xen",
        ) as fake_glance:
            xen_driver._xen_requested(mock.Mock(), instance)
            xen_driver._xen_requested(mock.Mock(), instance)
        self.assertEqual(fake_glance.call_count, 1)

    def test_upstream_img_hv_type_xen_does_NOT_activate(self):
        """Traditional img_hv_type=xen must not activate the driver.

        Only the plugin-namespaced ``ospc:hw_hypervisor_interface=xen``
        property flips the switch, so images moved between clouds are
        never unintentionally interpreted as Xen-emulated.
        """
        instance = _make_instance(
            sysmeta={
                "image_img_hv_type": "xen",
                "image_hw_vm_mode": "xen",
                "image_hw_hypervisor_interface": "xen",  # non-namespaced
            }
        )
        with mock.patch.object(
            xen_driver,
            "_fetch_image_opt_in",
            return_value=None,
        ):
            self.assertFalse(xen_driver._xen_requested(mock.Mock(), instance))

    def test_other_value_does_not_activate(self):
        instance = _make_instance(
            sysmeta={
                "image_ospc:hw_hypervisor_interface": "kvm",
            }
        )
        with mock.patch.object(
            xen_driver,
            "_fetch_image_opt_in",
            return_value=None,
        ):
            self.assertFalse(xen_driver._xen_requested(mock.Mock(), instance))


# ---------------------------------------------------------------------------
# Host capability probe tests.
# ---------------------------------------------------------------------------


class HostProbeTest(unittest.TestCase):

    def _ioctl_sequence(self, create_vm_result, config_result):
        """Build a fake fcntl.ioctl that dispatches by ioctl number.

        ``KVM_CREATE_VM`` returns ``create_vm_result`` (a new fd, or
        raises if it's an ``OSError``). ``KVM_XEN_HVM_CONFIG`` returns
        ``config_result`` (0, or raises).
        """

        def _ioctl(fd, request, arg):
            if request == xen_driver.KVM_CREATE_VM:
                if isinstance(create_vm_result, BaseException):
                    raise create_vm_result
                return create_vm_result
            if request == xen_driver.KVM_XEN_HVM_CONFIG:
                if isinstance(config_result, BaseException):
                    raise config_result
                return config_result
            raise AssertionError("unexpected ioctl request 0x%x" % request)

        return _ioctl

    def test_kvm_xen_hvm_capable_true_when_config_ioctl_succeeds(self):
        with mock.patch.object(
            xen_driver.os,
            "open",
            return_value=42,
        ), mock.patch.object(
            xen_driver.os,
            "close",
        ), mock.patch.object(
            xen_driver.fcntl,
            "ioctl",
            side_effect=self._ioctl_sequence(create_vm_result=99, config_result=0),
        ):
            self.assertTrue(xen_driver._kvm_xen_hvm_capable())

    def test_kvm_xen_hvm_capable_false_when_config_ioctl_rejects(self):
        with mock.patch.object(
            xen_driver.os,
            "open",
            return_value=42,
        ), mock.patch.object(
            xen_driver.os,
            "close",
        ), mock.patch.object(
            xen_driver.fcntl,
            "ioctl",
            side_effect=self._ioctl_sequence(
                create_vm_result=99,
                config_result=OSError(22, "Invalid argument"),
            ),
        ):
            self.assertFalse(xen_driver._kvm_xen_hvm_capable())

    def test_kvm_xen_hvm_capable_false_when_create_vm_fails(self):
        with mock.patch.object(
            xen_driver.os,
            "open",
            return_value=42,
        ), mock.patch.object(
            xen_driver.os,
            "close",
        ), mock.patch.object(
            xen_driver.fcntl,
            "ioctl",
            side_effect=self._ioctl_sequence(
                create_vm_result=OSError(13, "Permission denied"),
                config_result=0,  # unreachable
            ),
        ):
            self.assertFalse(xen_driver._kvm_xen_hvm_capable())

    def test_kvm_xen_hvm_capable_false_on_missing_dev_kvm(self):
        with mock.patch.object(
            xen_driver.os,
            "open",
            side_effect=FileNotFoundError("/dev/kvm"),
        ):
            self.assertFalse(xen_driver._kvm_xen_hvm_capable())

    def test_kvm_xen_hvm_config_ioctl_number_is_iow_encoded(self):
        """Guard the _IOC encoding — a bad number silently reads nothing."""
        # _IOW(KVMIO=0xAE, 0x7A, sizeof(struct kvm_xen_hvm_config)=56)
        expected = (1 << 30) | (56 << 16) | (0xAE << 8) | 0x7A
        self.assertEqual(xen_driver.KVM_XEN_HVM_CONFIG, expected)

    def test_init_host_warns_but_does_not_raise_on_old_qemu(self):
        d = _make_driver(xen_capable=False)
        d._host = mock.MagicMock()
        d._host.has_min_version.return_value = False
        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "init_host",
            return_value=None,
        ), mock.patch.object(
            xen_driver,
            "_kvm_xen_hvm_capable",
            return_value=False,
        ):
            d.init_host("host")  # must not raise
        self.assertFalse(d._xen_capable)

    def test_init_host_sets_capable_when_both_probes_pass(self):
        d = _make_driver(xen_capable=False)
        d._host = mock.MagicMock()
        d._host.has_min_version.return_value = True
        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "init_host",
            return_value=None,
        ), mock.patch.object(
            xen_driver,
            "_kvm_xen_hvm_capable",
            return_value=True,
        ):
            d.init_host("host")
        self.assertTrue(d._xen_capable)

    def test_init_host_capable_false_when_kvm_missing(self):
        d = _make_driver(xen_capable=False)
        d._host = mock.MagicMock()
        d._host.has_min_version.return_value = True
        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "init_host",
            return_value=None,
        ), mock.patch.object(
            xen_driver,
            "_kvm_xen_hvm_capable",
            return_value=False,
        ):
            d.init_host("host")
        self.assertFalse(d._xen_capable)

    def test_init_host_assume_capable_short_circuits_probe(self):
        """K8s pod case: operator declares capability, probe is skipped.

        A real /dev/kvm probe from inside an unprivileged pod returns
        False (EACCES) even when libvirtd on the host can run Xen
        guests. ``assume_capable`` must accept that declaration and
        *not* call the probe at all — if it did, the probe's False
        would override and the trait would stay hidden.
        """
        d = _make_driver(xen_capable=False)
        d._host = mock.MagicMock()
        d._host.has_min_version.return_value = True
        xen_driver.CONF.set_override(
            "kvm_xen_probe",
            "assume_capable",
            group="xen_emulation",
        )
        try:
            with mock.patch.object(
                libvirt_driver.LibvirtDriver,
                "init_host",
                return_value=None,
            ), mock.patch.object(
                xen_driver,
                "_kvm_xen_hvm_capable",
                side_effect=AssertionError("probe must be skipped"),
            ):
                d.init_host("host")
        finally:
            xen_driver.CONF.clear_override(
                "kvm_xen_probe",
                group="xen_emulation",
            )
        self.assertTrue(d._xen_capable)

    def test_init_host_assume_uncapable_forces_false(self):
        """Operator escape hatch: hide a partially-capable host."""
        d = _make_driver(xen_capable=False)
        d._host = mock.MagicMock()
        d._host.has_min_version.return_value = True
        xen_driver.CONF.set_override(
            "kvm_xen_probe",
            "assume_uncapable",
            group="xen_emulation",
        )
        try:
            with mock.patch.object(
                libvirt_driver.LibvirtDriver,
                "init_host",
                return_value=None,
            ), mock.patch.object(
                xen_driver,
                "_kvm_xen_hvm_capable",
                side_effect=AssertionError("probe must be skipped"),
            ):
                d.init_host("host")
        finally:
            xen_driver.CONF.clear_override(
                "kvm_xen_probe",
                group="xen_emulation",
            )
        self.assertFalse(d._xen_capable)

    def test_init_host_assume_capable_still_requires_qemu_version(self):
        """``assume_capable`` overrides only the KVM side, not QEMU."""
        d = _make_driver(xen_capable=False)
        d._host = mock.MagicMock()
        d._host.has_min_version.return_value = False  # QEMU < 8.1
        xen_driver.CONF.set_override(
            "kvm_xen_probe",
            "assume_capable",
            group="xen_emulation",
        )
        try:
            with mock.patch.object(
                libvirt_driver.LibvirtDriver,
                "init_host",
                return_value=None,
            ):
                d.init_host("host")
        finally:
            xen_driver.CONF.clear_override(
                "kvm_xen_probe",
                group="xen_emulation",
            )
        self.assertFalse(d._xen_capable)

    def test_kvm_xen_probe_opt_default_is_auto(self):
        self.assertEqual("auto", xen_driver.CONF.xen_emulation.kvm_xen_probe)


# ---------------------------------------------------------------------------
# _get_guest_xml tests (XML injection and inertness).
# ---------------------------------------------------------------------------


class GetGuestXmlTest(unittest.TestCase):

    def setUp(self):
        xen_driver._opt_in_cache.clear()

    def test_xen_args_injected_when_opt_in_property_set(self):
        parent_xml = "<domain type='kvm'><name>foo</name></domain>"
        instance = _make_instance(
            sysmeta={
                "image_ospc:hw_hypervisor_interface": "xen",
            }
        )
        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "_get_guest_xml",
            return_value=parent_xml,
        ):
            d = _make_driver(xen_capable=True)
            result = d._get_guest_xml(
                mock.Mock(),
                instance,
                None,
                _make_disk_info(),
                _make_image_meta(),
            )
        self.assertIn("xmlns:qemu=", result)
        self.assertIn("<qemu:commandline>", result)
        self.assertIn("-accel", result)
        self.assertIn("xen-version=0x40011", result)
        self.assertIn("kernel-irqchip=split", result)

    def test_xml_identical_to_parent_for_normal_guest(self):
        """Inertness: untagged guests must get byte-equal XML."""
        parent_xml = (
            "<domain type='kvm'>"
            "<name>foo</name>"
            "<uuid>12345678-1234-1234-1234-123456789012</uuid>"
            "<devices><disk type='file'/></devices>"
            "</domain>"
        )
        instance = _make_instance(sysmeta={})  # untagged
        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "_get_guest_xml",
            return_value=parent_xml,
        ), mock.patch.object(
            xen_driver,
            "_fetch_image_opt_in",
            return_value=None,
        ):
            d = _make_driver(xen_capable=True)
            result = d._get_guest_xml(
                mock.Mock(),
                instance,
                None,
                _make_disk_info(),
                _make_image_meta(),
            )
        self.assertEqual(parent_xml, result)

    def test_xml_unchanged_even_when_host_is_xen_capable_if_untagged(self):
        """Being Xen-capable is not sufficient — image must opt in."""
        parent_xml = "<domain type='kvm'><name>bar</name></domain>"
        instance = _make_instance(
            sysmeta={
                "image_ospc:hw_hypervisor_interface": "no",
            }
        )
        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "_get_guest_xml",
            return_value=parent_xml,
        ), mock.patch.object(
            xen_driver,
            "_fetch_image_opt_in",
            return_value=None,
        ):
            d = _make_driver(xen_capable=True)
            result = d._get_guest_xml(
                mock.Mock(),
                instance,
                None,
                _make_disk_info(),
                _make_image_meta(),
            )
        self.assertEqual(parent_xml, result)


# ---------------------------------------------------------------------------
# _get_guest_config tests (per-guest activation, bus/vif rewrite).
# ---------------------------------------------------------------------------


class GetGuestConfigTest(unittest.TestCase):

    def setUp(self):
        xen_driver._opt_in_cache.clear()

    def test_spawn_raises_only_for_xen_tagged_on_uncapable_host(self):
        d = _make_driver(xen_capable=False)

        # Untagged → parent is called, no raise.
        parent_guest = mock.Mock()
        parent_guest.devices = []
        instance_untagged = _make_instance(sysmeta={})
        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "_get_guest_config",
            return_value=parent_guest,
        ), mock.patch.object(
            xen_driver,
            "_fetch_image_opt_in",
            return_value=None,
        ):
            result = d._get_guest_config(
                instance_untagged,
                None,
                _make_image_meta(),
                _make_disk_info(),
                context=mock.Mock(),
            )
        self.assertIs(result, parent_guest)

        # Tagged → raises InternalError.
        from nova import exception

        instance_tagged = _make_instance(
            sysmeta={
                "image_ospc:hw_hypervisor_interface": "xen",
            }
        )
        self.assertRaises(
            exception.InternalError,
            d._get_guest_config,
            instance_tagged,
            None,
            _make_image_meta(),
            _make_disk_info(),
            context=mock.Mock(),
        )

    def test_xen_tagged_guest_config_left_unchanged(self):
        """Driver no longer rewrites disk bus / NIC model.

        libvirt's QEMU driver hard-rejects bus='xen' on a kvm domain
        (qemu_command.c:qemuBuildDiskDeviceProps), so any rewrite the
        driver did would just shift the failure from Nova-blockinfo to
        libvirt-validation. The only Xen-specific bit the driver still
        emits is the ``-accel`` injection in _get_guest_xml. Disk and
        VIF stay virtio; the captured image is responsible for shipping
        virtio drivers in its initramfs.
        """
        d = _make_driver(xen_capable=True)

        disk = vconfig.LibvirtConfigGuestDisk()
        disk.target_bus = "virtio"
        disk.target_dev = "vda"
        iface = vconfig.LibvirtConfigGuestInterface()
        iface.model = "virtio"

        parent_guest = mock.Mock()
        parent_guest.devices = [disk, iface]

        instance = _make_instance(sysmeta={"image_ospc:hw_hypervisor_interface": "xen"})
        image_meta = _make_image_meta()

        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "_get_guest_config",
            return_value=parent_guest,
        ):
            result = d._get_guest_config(
                instance,
                None,
                image_meta,
                _make_disk_info(),
                context=mock.Mock(),
            )

        self.assertIs(result, parent_guest)
        # No bus/dev/model rewriting.
        self.assertEqual(disk.target_bus, "virtio")
        self.assertEqual(disk.target_dev, "vda")
        self.assertEqual(iface.model, "virtio")

    def test_non_hvm_vm_mode_rejected(self):
        d = _make_driver(xen_capable=True)
        instance = _make_instance(
            sysmeta={
                "image_ospc:hw_hypervisor_interface": "xen",
            }
        )
        image_meta = _make_image_meta(hw_vm_mode="xen")  # must be 'hvm'

        from nova import exception

        self.assertRaises(
            exception.InvalidMetadata,
            d._get_guest_config,
            instance,
            None,
            image_meta,
            _make_disk_info(),
            context=mock.Mock(),
        )

    def test_hvm_vm_mode_accepted(self):
        d = _make_driver(xen_capable=True)
        instance = _make_instance(
            sysmeta={
                "image_ospc:hw_hypervisor_interface": "xen",
            }
        )
        image_meta = _make_image_meta(hw_vm_mode="hvm")

        parent_guest = mock.Mock()
        parent_guest.devices = []
        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "_get_guest_config",
            return_value=parent_guest,
        ):
            result = d._get_guest_config(
                instance,
                None,
                image_meta,
                _make_disk_info(),
                context=mock.Mock(),
            )
        self.assertIs(result, parent_guest)


# ---------------------------------------------------------------------------
# Inertness contract tests.
# ---------------------------------------------------------------------------


class InertnessContractTest(unittest.TestCase):

    def test_no_import_time_mutation_of_bus_or_vif_allowlists(self):
        """Source must never mutate the global SUPPORTED_* dicts.

        An earlier draft of the plan widened
        ``blockinfo.SUPPORTED_DEVICE_BUSES['kvm']`` and
        ``vif.SUPPORTED_VIF_MODELS['kvm']`` at import. That would have
        leaked into placement trait reporting and validation for every
        guest the driver renders. We scan the AST (not raw text) so
        explanatory comments don't false-positive.
        """
        import ast
        from nova.virt import nova_rxt as pkg

        forbidden = {"SUPPORTED_DEVICE_BUSES", "SUPPORTED_VIF_MODELS"}

        def _mutates(name, tree):
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if _target_mentions(t, name):
                            return True
                elif isinstance(node, ast.AugAssign):
                    if _target_mentions(node.target, name):
                        return True
                elif isinstance(node, ast.Call):
                    if _call_mutates(node.func, name):
                        return True
            return False

        def _target_mentions(t, name):
            if isinstance(t, ast.Name) and t.id == name:
                return True
            if isinstance(t, ast.Attribute) and t.attr == name:
                return True
            if isinstance(t, ast.Subscript):
                return _target_mentions(t.value, name)
            if isinstance(t, (ast.Tuple, ast.List)):
                return any(_target_mentions(e, name) for e in t.elts)
            return False

        def _call_mutates(func, name):
            # foo.SUPPORTED_X.update(...) or SUPPORTED_X.update(...) etc.
            if not isinstance(func, ast.Attribute):
                return False
            if func.attr not in {"update", "setdefault", "pop", "__setitem__", "clear"}:
                return False
            obj = func.value
            if isinstance(obj, ast.Name) and obj.id == name:
                return True
            if isinstance(obj, ast.Attribute) and obj.attr == name:
                return True
            return False

        for mod in (xen_driver, pkg):
            src = inspect.getsource(mod)
            tree = ast.parse(src)
            for name in forbidden:
                self.assertFalse(
                    _mutates(name, tree),
                    msg=(
                        "%s mutates %s — this leaks into stock "
                        "LibvirtDriver behavior. See the inertness "
                        "contract in plan/nova-xen-driver.md." % (mod.__name__, name)
                    ),
                )

    def test_per_guest_trait_methods_not_overridden(self):
        """Per-guest trait-reporting methods must be inherited verbatim.

        ``_get_vif_model_traits`` and ``_get_storage_bus_traits``
        iterate the global ``SUPPORTED_*`` dicts to report traits for
        every guest on the host. Overriding them would change placement
        for all workloads, not just Xen-tagged ones — a direct
        violation of the inertness contract.

        ``update_provider_tree`` is intentionally *not* in this list:
        it is overridden to add the optional
        ``CUSTOM_COMPUTE_XEN_EMULATION`` trait, but that override is
        gated on two guards (operator opt-in AND host capability) and
        is a pure pass-through when either is False. Inertness for
        that override is covered by
        :meth:`test_update_provider_tree_inert_when_publish_trait_false`.
        """
        sub = xen_driver.XenEmulationDriver
        for name in (
            "_get_vif_model_traits",
            "_get_storage_bus_traits",
        ):
            self.assertIsNone(
                sub.__dict__.get(name),
                msg=(
                    "XenEmulationDriver defines %s — this overrides "
                    "parent and breaks placement inertness." % name
                ),
            )

    def test_overrides_accept_parent_positional_signature(self):
        """Nova's spawn() calls these methods with ALL-positional args.

        Regression guard: we used to name ``old_guest`` (not in any
        released Nova) and forward it positionally to super, which
        exploded at spawn time with ``takes from 6 to 10 positional
        arguments but 12 were given``. The fix was to switch the tail
        of our overrides to ``*args, **kwargs`` and drop the phantom
        param.

        Nova 2024.1 and 2025.1 differ only in whether they pass
        ``share_info`` as a trailing positional. Both call shapes must
        work so a single image can deploy against both releases (the
        Dockerfile builds for both). Test is intentionally loud about
        its purpose so nobody re-introduces a named-param tail "for
        clarity" and re-breaks spawn.
        """
        d = _make_driver(xen_capable=False)
        instance = _make_instance(sysmeta={})  # untagged → pure passthrough

        parent_guest = mock.Mock()
        parent_guest.devices = []

        # _get_guest_config — (instance, network_info, image_meta,
        # disk_info, rescue, block_device_info, context, mdevs,
        # accel_info [, share_info]). 2024.1 = 9, 2025.1 = 10.
        # _get_guest_xml — (context, instance, network_info, disk_info,
        # image_meta, rescue, block_device_info, mdevs, accel_info
        # [, share_info]). Same 9 vs 10.
        config_base = (
            instance,
            None,
            _make_image_meta(),
            _make_disk_info(),
            None,
            None,
            mock.Mock(),
            None,
            None,
        )
        xml_base = (
            mock.Mock(),
            instance,
            None,
            _make_disk_info(),
            _make_image_meta(),
            None,
            None,
            None,
            None,
        )
        cases = [
            ("2024.1", config_base, xml_base),
            ("2025.1", config_base + (None,), xml_base + (None,)),  # +share_info
        ]

        for label, config_args, xml_args in cases:
            with mock.patch.object(
                libvirt_driver.LibvirtDriver,
                "_get_guest_config",
                return_value=parent_guest,
            ), mock.patch.object(
                libvirt_driver.LibvirtDriver,
                "_get_guest_xml",
                return_value="<domain type='kvm'/>",
            ), mock.patch.object(
                xen_driver,
                "_fetch_image_opt_in",
                return_value=None,
            ):
                try:
                    d._get_guest_config(*config_args)
                    d._get_guest_xml(*xml_args)
                except TypeError as e:
                    self.fail(
                        "Nova %s positional call shape rejected by "
                        "override: %s" % (label, e)
                    )

    def test_typo_hw_disk_bus_xen_on_untagged_image_still_rejected(self):
        """Untagged image with hw_disk_bus=xen hits parent's validator.

        Because we do not globally widen ``SUPPORTED_DEVICE_BUSES``,
        parent's ``_get_guest_config`` should reject it in the normal
        code path. We simulate this by asserting our override delegates
        directly to parent (no pre-translation) when no opt-in is set.
        """
        d = _make_driver(xen_capable=True)
        instance = _make_instance(sysmeta={})  # untagged
        image_meta = _make_image_meta(hw_disk_bus="xen")  # typo on untagged

        class _ParentRejected(Exception):
            pass

        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "_get_guest_config",
            side_effect=_ParentRejected("xen not in kvm allow-list"),
        ), mock.patch.object(
            xen_driver,
            "_fetch_image_opt_in",
            return_value=None,
        ):
            self.assertRaises(
                _ParentRejected,
                d._get_guest_config,
                instance,
                None,
                image_meta,
                _make_disk_info(),
                context=mock.Mock(),
            )
        # Confirm our override did not pre-translate (image_meta intact).
        self.assertEqual(image_meta.properties.hw_disk_bus, "xen")


# ---------------------------------------------------------------------------
# Small unit helpers.
# ---------------------------------------------------------------------------


class GetPropTest(unittest.TestCase):

    def test_get_set_field(self):
        props = _FakeImageMetaProps(hw_disk_bus="xen")
        self.assertEqual(xen_driver._get_prop(props, "hw_disk_bus"), "xen")

    def test_unset_field_returns_default(self):
        props = _FakeImageMetaProps()
        self.assertIsNone(xen_driver._get_prop(props, "hw_disk_bus"))
        self.assertEqual(
            xen_driver._get_prop(props, "hw_disk_bus", "fallback"),
            "fallback",
        )

    def test_no_obj_attr_is_set_method_falls_back_to_getattr(self):
        class _Plain:
            pass

        p = _Plain()
        p.hw_disk_bus = "virtio"
        self.assertEqual(
            xen_driver._get_prop(p, "hw_disk_bus"),
            "virtio",
        )
        self.assertIsNone(xen_driver._get_prop(p, "nonexistent"))


# ---------------------------------------------------------------------------
# Optional placement trait.
# ---------------------------------------------------------------------------


class UpdateProviderTreeTest(unittest.TestCase):

    def setUp(self):
        # Paranoid: make sure no previous test left an override in place.
        xen_driver.CONF.clear_override("publish_trait", group="xen_emulation")

    def tearDown(self):
        xen_driver.CONF.clear_override("publish_trait", group="xen_emulation")

    def test_trait_name_has_custom_prefix(self):
        """Placement rejects custom traits without the CUSTOM_ prefix."""
        self.assertTrue(
            xen_driver.TRAIT_XEN_EMULATION.startswith("CUSTOM_"),
            "Custom traits must be prefixed CUSTOM_; got %r"
            % xen_driver.TRAIT_XEN_EMULATION,
        )

    def test_publish_trait_default_is_true(self):
        """Default ON: trait advertised on Xen-capable hosts.

        Gating on the capability probe keeps this honest — non-Xen
        hosts still publish nothing. Operators who want Placement
        untouched can set ``publish_trait = false``.
        """
        self.assertTrue(xen_driver.CONF.xen_emulation.publish_trait)

    def test_update_provider_tree_inert_when_publish_trait_false(self):
        """Opt-out case: pass through to parent, add nothing.

        Operators who set ``publish_trait = false`` must see zero
        deviation from stock, even on a Xen-capable host.
        """
        xen_driver.CONF.set_override(
            "publish_trait",
            False,
            group="xen_emulation",
        )
        d = _make_driver(xen_capable=True)
        provider_tree = mock.Mock()

        super_calls = []

        def _fake_super(self_, pt, node, alloc=None, **kw):
            super_calls.append((pt, node, alloc, kw))

        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "update_provider_tree",
            new=_fake_super,
        ):
            d.update_provider_tree(
                provider_tree,
                "node-1",
                allocations={"x": "y"},
            )

        # Parent was called with the user's exact args.
        self.assertEqual(
            super_calls,
            [(provider_tree, "node-1", {"x": "y"}, {})],
        )
        # Driver did not touch the tree on its own.
        provider_tree.add_traits.assert_not_called()
        provider_tree.update_traits.assert_not_called()

    def test_update_provider_tree_publishes_trait_when_opted_in_and_capable(
        self,
    ):
        xen_driver.CONF.set_override(
            "publish_trait",
            True,
            group="xen_emulation",
        )
        d = _make_driver(xen_capable=True)
        provider_tree = mock.Mock()

        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "update_provider_tree",
            new=lambda self, pt, node, alloc=None, **kw: None,
        ):
            d.update_provider_tree(provider_tree, "node-1")

        provider_tree.add_traits.assert_called_once_with(
            "node-1",
            xen_driver.TRAIT_XEN_EMULATION,
        )

    def test_update_provider_tree_silent_when_uncapable(self):
        """Opting in is not a license to lie about capability.

        Even with ``publish_trait=True``, an uncapable host must not
        advertise the trait — otherwise the scheduler would land
        Xen-tagged guests on hosts where their build would then fail
        with InternalError.
        """
        xen_driver.CONF.set_override(
            "publish_trait",
            True,
            group="xen_emulation",
        )
        d = _make_driver(xen_capable=False)
        provider_tree = mock.Mock()

        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "update_provider_tree",
            new=lambda self, pt, node, alloc=None, **kw: None,
        ):
            d.update_provider_tree(provider_tree, "node-1")

        provider_tree.add_traits.assert_not_called()

    def test_update_provider_tree_super_is_called_before_trait(self):
        """Parent must run first; trait is added to whatever it produced."""
        xen_driver.CONF.set_override(
            "publish_trait",
            True,
            group="xen_emulation",
        )
        d = _make_driver(xen_capable=True)
        provider_tree = mock.Mock()
        order = []

        def _fake_super(self_, pt, node, alloc=None, **kw):
            order.append("super")

        def _record_add(name, *traits):
            order.append(("add", name, traits))

        provider_tree.add_traits.side_effect = _record_add

        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "update_provider_tree",
            new=_fake_super,
        ):
            d.update_provider_tree(provider_tree, "node-1")

        self.assertEqual(
            order,
            [
                "super",
                ("add", "node-1", (xen_driver.TRAIT_XEN_EMULATION,)),
            ],
        )

    def test_update_provider_tree_forwards_extra_kwargs(self):
        """Future-proof: unknown kwargs reach parent untouched."""
        d = _make_driver(xen_capable=True)
        received = {}

        def _fake_super(self_, pt, node, alloc=None, **kw):
            received["alloc"] = alloc
            received["kwargs"] = kw

        with mock.patch.object(
            libvirt_driver.LibvirtDriver,
            "update_provider_tree",
            new=_fake_super,
        ):
            d.update_provider_tree(
                mock.Mock(),
                "node-X",
                allocations=None,
                future_kwarg="passthrough-value",
            )

        self.assertEqual(received["alloc"], None)
        self.assertEqual(received["kwargs"], {"future_kwarg": "passthrough-value"})


# ---------------------------------------------------------------------------
# CONF wiring.
# ---------------------------------------------------------------------------


class ConfOptionsTest(unittest.TestCase):

    def test_qemu_args_default_includes_xen_4_10_abi(self):
        self.assertIn(
            "xen-version=0x40011",
            " ".join(xen_driver.CONF.xen_emulation.qemu_args),
        )

    def test_qemu_args_default_includes_kernel_irqchip_split(self):
        # QEMU's Xen emulator refuses to init with the default in-kernel
        # irqchip — missing this turns every Xen-tagged build into a hard
        # failure even on an otherwise capable host.
        self.assertIn(
            "kernel-irqchip=split",
            " ".join(xen_driver.CONF.xen_emulation.qemu_args),
        )

    def test_qemu_args_default_does_not_include_phantom_evtchn_upcall(self):
        # 'xen-evtchn-upcall' is not a property of QEMU's kvm-accel
        # object. Supplying it makes QEMU refuse to start. If a future
        # QEMU ever adds such a property we can revisit; for now, guard
        # against regressing back to the historical bogus default.
        self.assertNotIn(
            "xen-evtchn-upcall",
            " ".join(xen_driver.CONF.xen_emulation.qemu_args),
        )

    def test_injected_xml_uses_configured_args(self):
        """Injected <qemu:arg> values come from CONF, not a hardcoded list."""
        xen_driver.CONF.set_override(
            "qemu_args",
            ["-magic", "argstring"],
            group="xen_emulation",
        )
        try:
            parent_xml = "<domain type='kvm'><name>f</name></domain>"
            instance = _make_instance(
                sysmeta={
                    "image_ospc:hw_hypervisor_interface": "xen",
                }
            )
            with mock.patch.object(
                libvirt_driver.LibvirtDriver,
                "_get_guest_xml",
                return_value=parent_xml,
            ):
                d = _make_driver(xen_capable=True)
                result = d._get_guest_xml(
                    mock.Mock(),
                    instance,
                    None,
                    _make_disk_info(),
                    _make_image_meta(),
                )
            root = xmlinject.etree.fromstring(result.encode("utf-8"))
            cmdline = root[-1]
            values = [c.get("value") for c in cmdline]
            self.assertEqual(values, ["-magic", "argstring"])
        finally:
            xen_driver.CONF.clear_override(
                "qemu_args",
                group="xen_emulation",
            )


if __name__ == "__main__":
    unittest.main()
