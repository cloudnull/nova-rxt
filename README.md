# Nova RXT - Xen Emulation Driver

An optional, pip-installable Nova compute driver that enables
**QEMU 8.1+ Xen emulation** for PVHVM guests — Nova schedules the guest,
KVM runs it, and QEMU exposes Xen hypercalls, event channels, grant tables,
xenbus, and xenstore so unmodified Xen PV drivers bind cleanly. No
hypervisor-level data conversion is required.

Shipped as a `LibvirtDriver` subclass: all imaging, networking, volumes,
migration plumbing, and lifecycle behavior is inherited unchanged. The
driver activates **per instance** on an explicit image opt-in, and it is
a verbatim passthrough for every other guest.

## Requirements

- Nova (libvirt driver) — same version as on the compute host.
- QEMU >= 8.1.0 (for the built-in Xen ABI emulator).
- Linux kernel >= 6.0 with `KVM_CAP_XEN_HVM` and
  `KVM_XEN_HVM_CONFIG_SHARED_INFO`.
- `<qemu:commandline>` must be permitted by the host's security driver
  (see AppArmor / SELinux note below).

## Install

```bash
pip install git+https://github.com/cloudnull/nova-rxt.git
```

## Enable on a compute host

```ini
# /etc/nova/nova.conf
[DEFAULT]
compute_driver = nova_rxt.driver.XenEmulationDriver
```

Restart `nova-compute`. At startup the driver soft-probes the host for
QEMU 8.1+ and `KVM_CAP_XEN_HVM`. On failure it logs a WARNing and keeps
serving normal KVM guests; only a *Xen-tagged* build fails on an
uncapable host.

## Optional tuning

```ini
[xen_emulation]
# QEMU args injected under <qemu:commandline> for Xen-tagged guests.
# Defaults target the Xen 4.10 ABI — override if a newer guest kernel
# probes for a different version.
qemu_args =
    -accel, kvm,xen-version=0x40011,kernel-irqchip=split
```

### Optional placement trait configuration

The driver advertises `CUSTOM_COMPUTE_XEN_EMULATION` on the compute
node's resource provider in Placement so operators can route
Xen-tagged guests to Xen-capable hosts with one line of image or
flavor metadata.

**Default: ON, but gated on the capability probe.** On a host that
lacks QEMU 8.1+ or `KVM_CAP_XEN_HVM`, the trait is **never**
advertised — the driver will not lie about what it can run. On a
Xen-capable host the trait appears, but it has **zero scheduling
effect** until some image or flavor asks for it. No existing
workload changes unless an operator opts into the trait from the
scheduling side.

Operators who want Placement untouched (e.g. during a phased rollout,
or because they route via host aggregates) can set:

```ini
[xen_emulation]
publish_trait = false
```

### Kubernetes / containerised nova-compute

When `nova-compute` runs in a pod that can't open `/dev/kvm` (common:
the host node has `/dev/kvm` but the pod's user isn't in the `kvm`
group), the driver's `/dev/kvm` probe returns False even though
`libvirtd` on the host — which is who actually runs QEMU — is fully
capable. The probe is asking the wrong kernel. Declare the host's
real capability instead:

```ini
[xen_emulation]
kvm_xen_probe = assume_capable
```

Choices are `auto` (default; run the ioctl probe), `assume_capable`
(skip the probe, trust the operator), or `assume_uncapable` (force
the trait off without touching `publish_trait`). Spawn-time still
raises `InternalError` if a Xen-tagged build genuinely fails on this
host, so `assume_capable` loses the scheduling optimization on a lying
host but not the correctness backstop.

### Register the trait (optional)

Nova's scheduler report client auto-creates custom traits in Placement
the first time a compute node advertises one, so normally no manual
registration is needed. Pre-register only if you want to set
``trait:CUSTOM_COMPUTE_XEN_EMULATION=required`` on an image or flavor
**before** any Xen-capable compute has reported in:

```bash
openstack --os-placement-api-version 1.6 trait create \
    CUSTOM_COMPUTE_XEN_EMULATION
```

#### Select on the trait

Require it on an image:

```bash
openstack image set \
    --property trait:CUSTOM_COMPUTE_XEN_EMULATION=required \
    <image>
```

…or on a flavor:

```bash
openstack flavor set \
    --property trait:CUSTOM_COMPUTE_XEN_EMULATION=required \
    <flavor>
```

The scheduler will then only place matching instances on hosts that
publish the trait. Evacuation, rebuild, and cold migration all honor
it, so a Xen-tagged instance can't silently land on a stock-KVM host
and fail to find its PV drivers. The driver itself still refuses a
Xen-tagged build on an uncapable host with a clear `InternalError`,
so even without the trait the worst case is a loud failure, not a
silently broken guest.

## Tag an image

Exactly one plugin-namespaced property activates the driver for an
instance. Set it on the Glance image:

```bash
openstack image set \
    --property ospc:hw_hypervisor_interface=xen \
    <image>
```

- `ospc:hw_hypervisor_interface=xen` — the opt-in. **Any** other value
  (including `img_hv_type=xen`) leaves the driver inert.
- `hw_disk_bus=xen` — optional; when set alongside the opt-in, the
  driver overrides the parent's bus/target selection to emit
  `target_bus='xen'` and rewrites `vdX` → `xvdX` so Xen PV `blkfront`
  binds instead of `virtio-blk`.
- `hw_vif_model=xen` — optional; same idea for `xen-netfront`.

Untagged images boot under stock `LibvirtDriver` behavior. In
particular, `hw_disk_bus=xen` on an *untagged* image is still rejected
by `blockinfo.is_disk_bus_valid_for_virt` — the driver does **not**
widen the global allow-list.

## What the driver emits

For a Xen-tagged guest, the rendered domain XML keeps
`<domain type="kvm">` and `machine='pc'` but adds:

```xml
<domain type="kvm" xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0">
  ...
  <qemu:commandline>
    <qemu:arg value='-accel'/>
    <qemu:arg value='kvm,xen-version=0x40011,kernel-irqchip=split'/>
  </qemu:commandline>
</domain>
```

The guest's Xen PV drivers discover the hypervisor via CPUID leaf
`0x40000000` and the hypercall MSR that `-accel kvm,xen-version=…`
programs .

## Operator notes & known limitations

Surface these to operators so they choose the feature with eyes open:

- **AppArmor / SELinux**: `<qemu:commandline>` is rejected by Ubuntu's
  default `virt-aa-helper` profile. Either set `security_driver = "none"`
  in `qemu.conf`, or install a local override that permits the `-accel`
  arg string.
- **Evacuation / rebuild** onto a host *without* the plugin: the
  instance's `system_metadata` still carries
  `image_ospc:hw_hypervisor_interface=xen`, but a destination host
  running stock `LibvirtDriver` will render XML without the Xen args
  and PV drivers will fail to bind. Restrict Xen-tagged guests to a
  host aggregate, or wait for the upcoming `COMPUTE_XEN_EMULATION`
  placement trait.
- **Cold migration / resize**: destination must also run the plugin.
- **Live migration**: not supported in the first release (known QEMU
  Xen-mode gaps).
- **Snapshots / quiesce**: `virDomainFSFreeze` requires
  `qemu-guest-agent` over virtio-serial; Xen PV drivers don't expose an
  equivalent. Use `--property quiesce_unsupported_ok=true` when
  snapshotting, or treat snapshots as crash-consistent.
- **NUMA / hugepages**: inherited unchanged from `LibvirtDriver`, but
  Xen balloon behavior interacts with hugepage pinning differently from
  KVM virtio-balloon — smoke-test on your config.

## Out of scope

- Classic Xen PV / PVH guest shapes.
- Live migration of Xen-emulated guests.
- XenAPI / XAPI north-bound emulation.
- Xen grant-table PCI passthrough and SR-IOV.
- Non-x86 architectures.
- A scheduler filter.

## Development

```bash
tox -e py3     # unit tests
tox -e pep8    # flake8
```

The driver's remains inert unless configured on an image. this is
enforced by tests that assert byte-equality of rendered XML and
identity of reported placement traits against stock `LibvirtDriver`
for untagged images.
