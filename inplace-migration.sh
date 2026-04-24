#!/bin/bash
set -eo pipefail

GLANCE_URL="${GLANCE_URL:-$1}"
TOKEN="${TOKEN:-$2}"
ROOT_PARENT="${ROOT_PARENT:-$3}"

if [ -z "$GLANCE_URL" ] || [ -z "$TOKEN" ]; then
    echo "Usage: GLANCE_URL=... TOKEN=... $0" >&2
    echo "   or: $0 <glance_url> <token>" >&2
    exit 1
fi

if [ -z "$ROOT_PARENT" ]; then
    echo "Resolving root disk..."
    ROOT_SRC="$(findmnt -no SOURCE /)"
    ROOT_PARENT="$(lsblk -no PKNAME "$ROOT_SRC" 2>/dev/null | head -1 || true)"
    if [ -z "$ROOT_PARENT" ]; then
        echo "Error: could not resolve parent disk for root (${ROOT_SRC})." >&2
        echo "If root is on a dm/md device, image the underlying disk directly." >&2
        exit 1
    fi
fi
DISK_DEVICE="/dev/${ROOT_PARENT}"
echo "Root disk: ${DISK_DEVICE} (root fs on ${ROOT_SRC})"

# Sanity: must be a block device, must look like a whole disk
[ -b "$DISK_DEVICE" ] || { echo "Error: ${DISK_DEVICE} is not a block device." >&2; exit 1; }

# Freeze duration warning. Inform before committing.
DISK_BYTES=$(blockdev --getsize64 "$DISK_DEVICE")
DISK_GB=$(( DISK_BYTES / 1024 / 1024 / 1024 ))
echo ""
echo "About to freeze the root filesystem and clone ${DISK_GB} GB."
echo "All disk writes (logs, ssh history, monitoring) will block until"
echo "the upload completes."
echo ""
echo "Cancel within 5 seconds to abort..."

sleep 5

# All mounted partitions of this disk that need freezing. Anchored match.
MOUNTS=$(awk -v d="^${DISK_DEVICE}([0-9p]+)?\$" \
         '$1 ~ d {print $2}' /proc/mounts)

DISK_NAME="$(echo "$DISK_DEVICE" | sed 's|/|-|g')"
IMAGE_NAME="$(hostname -s)${DISK_NAME}-$(date +%s)"
PIPE="/run/image_upload_pipe.$$"   # tmpfs -- must NOT be on a frozen fs

FROZEN_MOUNTS=()
CURL_PID=""
IMAGE_ID=""

cleanup() {
    local rc=$?
    set +e
    trap - EXIT ERR INT TERM

    # Critical: unfreeze before anything else, in reverse order so / unfreezes last
    for (( i=${#FROZEN_MOUNTS[@]}-1; i>=0; i-- )); do
        fsfreeze --unfreeze "${FROZEN_MOUNTS[$i]}" 2>/dev/null \
            && echo "Unfroze: ${FROZEN_MOUNTS[$i]}"
    done

    if [ -n "$CURL_PID" ] && kill -0 "$CURL_PID" 2>/dev/null; then
        kill "$CURL_PID" 2>/dev/null
        wait "$CURL_PID" 2>/dev/null
    fi

    rm -f "$PIPE"

    if [ "$rc" -ne 0 ] && [ -n "$IMAGE_ID" ]; then
        echo "Failed; orphan Glance image: openstack image delete ${IMAGE_ID}" >&2
    fi
    exit "$rc"
}
trap cleanup EXIT ERR INT TERM

# Pre-create everything that would otherwise need disk I/O during the freeze.
# All commands inside the freeze window must already be in page cache.
echo "Pre-warming required binaries..."
type curl dd fsfreeze awk grep sed >/dev/null

# Update GRUB cmdline so the captured image boots with a usable console
# under KVM Xen emulation. XenServer guests commonly have console=hvc0,
# which has no backend in QEMU's xen-emu -- the guest boots but its console
# output goes nowhere. Appending tty0 + ttyS0 makes both the VNC graphical
# console and Nova's serial console work.
update_grub_cmdline() {
    local opts="console=tty0 console=ttyS0,115200n8"

    if command -v grubby >/dev/null 2>&1; then
        echo "Updating kernel entries via grubby..."
        grubby --remove-args="${opts}" --update-kernel=ALL >/dev/null 2>&1 || true
        grubby --args="${opts}" --update-kernel=ALL
        return 0
    fi

    if command -v update-grub >/dev/null 2>&1 && [ -f /etc/default/grub ]; then
        # Debian / Ubuntu 10.04 to current.
        if grep -qE '^GRUB_CMDLINE_LINUX=.*console=ttyS0' /etc/default/grub; then
            echo "GRUB cmdline already has console=ttyS0; skipping."
        else
            echo "Appending '${opts}' to GRUB_CMDLINE_LINUX..."
            cp -p /etc/default/grub /etc/default/grub.pre-migration
            if grep -qE '^GRUB_CMDLINE_LINUX=' /etc/default/grub; then
                # BRE form for portability; applies to every matching line
                # so stacked GRUB_CMDLINE_LINUX= overrides all get updated.
                sed -i \
                    "s|^\\(GRUB_CMDLINE_LINUX=\"\\)\\(.*\\)\\(\"\\)|\\1\\2 ${opts}\\3|" \
                    /etc/default/grub
            else
                echo "GRUB_CMDLINE_LINUX=\"${opts}\"" >> /etc/default/grub
            fi
        fi
        echo "Running update-grub..."
        update-grub
        return 0
    fi

    echo "Warning: no supported GRUB tool found (grubby or update-grub);" >&2
    echo "         captured image may not surface a console." >&2
}

update_grub_cmdline

# Convert root= kernel cmdline parameter from /dev/xvd*|sd*|vd*|hd* form to
# root=UUID=... so the kernel can find root on either Xen or KVM virtio.
#
# Without this, the captured image keeps "root=/dev/xvda1" in its kernel
# cmdline. Under KVM virtio the disk appears as /dev/vda1, so the kernel
# panics in initramfs with "Gave up waiting for root file system device /
# /dev/xvda1 does not exist". A UUID resolves identically on both platforms
# (same fs, same UUID), so the source XenServer instance stays bootable
# after this rewrite.
convert_grub_root_to_uuid() {
    if ! command -v blkid >/dev/null 2>&1; then
        echo "Warning: blkid not found; skipping GRUB root= rewrite." >&2
        return 0
    fi

    local root_src
    root_src="$(findmnt -no SOURCE /)"
    local root_uuid
    root_uuid=$(blkid -s UUID -o value "$root_src" 2>/dev/null) || true
    if [ -z "$root_uuid" ]; then
        echo "Warning: no UUID for ${root_src}; skipping GRUB root= rewrite." >&2
        return 0
    fi
    echo "Root filesystem UUID: ${root_uuid}"

    # grubby covers EL 6 legacy grub.conf and EL 7+/Fedora GRUB2 uniformly.
    # --args with an existing key replaces its value, so this overwrites
    # whatever root= was there before.
    if command -v grubby >/dev/null 2>&1; then
        echo "Setting root=UUID=${root_uuid} via grubby..."
        grubby --update-kernel=ALL --args="root=UUID=${root_uuid}"
    fi

    # Debian / Ubuntu: flip GRUB_DISABLE_LINUX_UUID off so update-grub emits
    # UUID-based root, then regenerate. Older Debian XenServer images often
    # set this to true, which is what forces the /dev/xvda1 path in the
    # first place.
    if [ -f /etc/default/grub ]; then
        if grep -qE '^GRUB_DISABLE_LINUX_UUID=true' /etc/default/grub; then
            echo "Setting GRUB_DISABLE_LINUX_UUID=false in /etc/default/grub..."
            sed -i 's|^GRUB_DISABLE_LINUX_UUID=true|GRUB_DISABLE_LINUX_UUID=false|' \
                /etc/default/grub
        fi
        if command -v update-grub >/dev/null 2>&1; then
            echo "Running update-grub..."
            update-grub
        fi
    fi

    # Direct rewrite as the safety net: scrub any remaining hardcoded
    # root=/dev/<old-name> in every grub config we can find. Idempotent --
    # the regex just won't match if root= already uses a UUID. Covers
    # GRUB2 grub.cfg, EL legacy grub.conf, very old menu.lst, and EFI.
    local cfg
    for cfg in \
        /boot/grub/grub.cfg \
        /boot/grub2/grub.cfg \
        /boot/grub/menu.lst \
        /boot/grub/grub.conf \
        /boot/efi/EFI/*/grub.cfg
    do
        [ -f "$cfg" ] || continue
        if grep -qE 'root=/dev/(xvd|sd|vd|hd)[a-z]+[0-9]*' "$cfg"; then
            echo "Rewriting root=/dev/* to root=UUID=${root_uuid} in ${cfg}"
            cp -p "$cfg" "${cfg}.pre-migration"
            sed -i -E \
                "s|root=/dev/(xvd|sd|vd|hd)[a-z]+[0-9]*|root=UUID=${root_uuid}|g" \
                "$cfg"
        fi
    done
}

convert_grub_root_to_uuid

# Install the runtime packages the migrated instance needs to behave
# under OpenStack:
#  - cloud-init: fetches metadata, SSH keys, and network config on first
#    boot. XenServer images often shipped without it because XenServer
#    used its own xenstore-based agent for the same job.
#  - qemu-guest-agent: lets Nova talk to the guest over virtio-serial
#    for fs-freeze snapshots, in-guest reboots, and IP reporting.
# Installed before the freeze so they're part of the captured image.
install_runtime_packages() {
    local pkgs=()
    command -v cloud-init >/dev/null 2>&1 || pkgs+=(cloud-init)
    if [ ! -x /usr/sbin/qemu-ga ] && [ ! -x /usr/bin/qemu-ga ]; then
        pkgs+=(qemu-guest-agent)
    fi

    if [ ${#pkgs[@]} -eq 0 ]; then
        echo "cloud-init and qemu-guest-agent already installed; skipping."
        return 0
    fi

    echo "Installing: ${pkgs[*]}..."
    if command -v dnf >/dev/null 2>&1; then
        dnf install -y "${pkgs[@]}"
    elif command -v yum >/dev/null 2>&1; then
        # EL 6 ships cloud-init only in EPEL; qemu-guest-agent is in base.
        if [ -f /etc/redhat-release ] && grep -qE 'release 6\.' /etc/redhat-release; then
            rpm -q epel-release >/dev/null 2>&1 || yum install -y epel-release
        fi
        yum install -y "${pkgs[@]}"
    elif command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
    else
        echo "Error: no supported package manager (dnf/yum/apt-get)." >&2
        return 1
    fi
}

install_runtime_packages

# Ensure the captured image's initramfs can probe virtio devices. The
# nova-rxt driver no longer rewrites disk/NIC bus to xen (libvirt's QEMU
# driver hard-rejects bus='xen' on a kvm domain), so the migrated guest
# sees disks and NICs as plain virtio under KVM Xen emulation. Without
# virtio_pci+virtio_blk in the initramfs an XenServer-origin image
# panics with "/dev/xvda1 does not exist" before userspace.
#
# Additive on the source: existing xen-blkfront / xen-netfront stay in
# place, so the source XenServer instance still boots on Xen -- at boot
# the kernel loads whichever frontends find a backend. Modules already
# shipped by the distro kernel are referenced by name; no extra package
# install is needed (CentOS 6+ and Ubuntu 10.04+ all ship virtio modules
# in their stock kernels).
update_initramfs_for_virtio() {
    local mods="virtio_pci virtio_blk virtio_net virtio_scsi"

    # Debian / Ubuntu 10.04 to current.
    if [ -d /etc/initramfs-tools ] && command -v update-initramfs >/dev/null 2>&1; then
        local f="/etc/initramfs-tools/modules"
        [ -f "$f" ] || touch "$f"
        local added=0
        for m in $mods; do
            if ! grep -qE "^${m}([[:space:]]|\$)" "$f"; then
                echo "$m" >> "$f"
                added=1
            fi
        done
        if [ "$added" -eq 1 ]; then
            echo "Added virtio modules to ${f}"
        else
            echo "${f} already lists virtio modules"
        fi
        echo "Running update-initramfs -u -k all..."
        update-initramfs -u -k all
        return 0
    fi

    # EL 6 / 7 / 8 / 9 -- dracut handles legacy grub.conf, GRUB2, and BLS
    # uniformly via --regenerate-all.
    if command -v dracut >/dev/null 2>&1; then
        local conf="/etc/dracut.conf.d/99-nova-rxt-virtio.conf"
        if [ ! -f "$conf" ]; then
            mkdir -p "$(dirname "$conf")"
            echo "add_drivers+=\" $mods \"" > "$conf"
            echo "Wrote ${conf}"
        else
            echo "${conf} already in place"
        fi
        echo "Running dracut --regenerate-all --force..."
        dracut --regenerate-all --force
        return 0
    fi

    echo "Warning: no supported initramfs tool found (update-initramfs or" >&2
    echo "         dracut); captured image likely won't have virtio drivers" >&2
    echo "         and will not boot under KVM Xen emulation." >&2
}

update_initramfs_for_virtio

# Convert /etc/fstab entries that reference /dev/xvd* (or /dev/sd*, /dev/vd*,
# /dev/hd*) to UUID= form so mounts resolve regardless of which disk
# nomenclature the kernel sees on boot. UUIDs work identically on Xen
# (xvda1's UUID) and on KVM virtio (vda1's UUID -- same backing fs, same
# UUID), so the source XenServer instance stays bootable after this
# rewrite. Backs up to /etc/fstab.pre-migration.
convert_fstab_to_uuid() {
    local fstab="/etc/fstab"
    [ -f "$fstab" ] || return 0
    if ! command -v blkid >/dev/null 2>&1; then
        echo "Warning: blkid not found; skipping fstab UUID conversion." >&2
        return 0
    fi

    cp -p "$fstab" "${fstab}.pre-migration"

    # Collect device-named first-fields, dedupe, look up UUID for each,
    # rewrite that exact device name in place. Anchored \b on the right
    # (via [[:space:]]) so /dev/sda1 doesn't accidentally match /dev/sda10.
    local devs
    devs=$(awk '
        !/^[[:space:]]*#/ &&
        $1 ~ /^\/dev\/(xvd|sd|vd|hd)[a-z]+[0-9]*$/ {print $1}
    ' "$fstab" | sort -u)

    local changed=0
    for dev in $devs; do
        local uuid
        uuid=$(blkid -s UUID -o value "$dev" 2>/dev/null) || true
        if [ -z "$uuid" ]; then
            echo "Skipping ${dev}: no UUID (unformatted, swap-without-uuid, etc.)"
            continue
        fi
        # Escape regex metas in the device path (only / really) and rewrite
        # only when the device is followed by whitespace (the field
        # separator), so /dev/sda1 doesn't match /dev/sda10.
        local esc
        esc=$(printf '%s' "$dev" | sed 's|/|\\/|g')
        sed -i "s|^${esc}\\([[:space:]]\\)|UUID=${uuid}\\1|" "$fstab"
        echo "Rewrote ${dev} to UUID=${uuid}"
        changed=1
    done
    if [ "$changed" = "0" ]; then
        echo "/etc/fstab already device-name-agnostic; no rewrites."
    fi
}

convert_fstab_to_uuid

if command -v xenstore >/dev/null 2>&1; then
    INSTANCE_UUID=$(xenstore read /local/domain/$(xenstore read domid)/name | sed 's/instance-//g')
    echo "XenStore is available. Instance UUID: $INSTANCE_UUID"
else
    INSTANCE_UUID="$(cat /sys/class/dmi/id/product_uuid 2>/dev/null || echo "unknown")"
fi

echo "Creating image record in Glance..."
CREATE_RESP=$(curl -fsS -X POST "${GLANCE_URL}/v2/images" \
    -H "X-Auth-Token: ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{
        \"name\": \"${IMAGE_NAME}\",
        \"disk_format\": \"raw\",
        \"container_format\": \"bare\",
        \"visibility\": \"private\",
        \"ospc:hw_hypervisor_interface\": \"xen\",
        \"ospc:instance_uuid\": \"${INSTANCE_UUID}\",
        \"hw_firmware_type\": \"bios\",
        \"hw_machine_type\": \"pc\",
        \"hw_qemu_guest_agent\": \"yes\"
    }")
IMAGE_ID=$(echo "$CREATE_RESP" | grep -oP '"id"\s*:\s*"\K[^"]+' | head -1)

if [ -z "$IMAGE_ID" ]; then
    echo "Error: image create failed:" >&2
    echo "$CREATE_RESP" >&2
    exit 1
fi
echo "Image ID: ${IMAGE_ID}"

mkfifo -m 600 "$PIPE"

# Reader first. curl blocks on empty FIFO until dd writes.
curl -fsS -X PUT "${GLANCE_URL}/v2/images/${IMAGE_ID}/file" \
    -H "X-Auth-Token: ${TOKEN}" \
    -H "Content-Type: application/octet-stream" \
    --upload-file "$PIPE" &
CURL_PID=$!

# Give curl a moment to establish the connection. If it dies here (auth,
# DNS, etc.), we want to know BEFORE we freeze.
sleep 2
if ! kill -0 "$CURL_PID" 2>/dev/null; then
    echo "Error: curl died before upload started." >&2
    exit 1
fi

echo "Trimming free space (no-op if storage backend doesn't honor it)..."
fstrim -av 2>&1 || echo "Trim failed or not supported; continuing anyway..."
sync

# Freeze. Order: non-root mounts first, root last. Unfreeze in reverse.
# This minimizes the time / is frozen, which is what hurts most.
NONROOT=()
HAS_ROOT=0
for m in $MOUNTS; do
    if [ "$m" = "/" ]; then HAS_ROOT=1; else NONROOT+=("$m"); fi
done

for m in "${NONROOT[@]}"; do
    if fsfreeze --freeze "$m" 2>/dev/null; then
        FROZEN_MOUNTS+=("$m")
        echo "Froze: $m"
    fi
done
if [ "$HAS_ROOT" = "1" ]; then
    if fsfreeze --freeze /; then
        FROZEN_MOUNTS+=("/")
        echo "Froze: /"
    else
        echo "Error: could not freeze /. Aborting." >&2
        exit 1
    fi
fi

echo "Streaming ${DISK_DEVICE} to Glance (do not touch the system)..."
dd if="${DISK_DEVICE}" bs=4M iflag=direct conv=sparse,noerror,sync of="${PIPE}" status=progress

# Unfreeze AS SOON as dd is done -- don't hold the freeze through the network drain
for (( i=${#FROZEN_MOUNTS[@]}-1; i>=0; i-- )); do
    fsfreeze --unfreeze "${FROZEN_MOUNTS[$i]}" 2>/dev/null \
        && echo "Unfroze: ${FROZEN_MOUNTS[$i]}"
done
FROZEN_MOUNTS=()

echo "dd done. Waiting for upload to drain..."
wait "$CURL_PID"; CURL_RC=$?; CURL_PID=""
if [ "$CURL_RC" -ne 0 ]; then
    echo "Error: upload failed (curl exit ${CURL_RC})" >&2
    exit "$CURL_RC"
fi

echo "Verifying Glance image status..."
for _ in 1 2 3 4 5 6 7 8 9 10; do
    STATUS=$(curl -fsS "${GLANCE_URL}/v2/images/${IMAGE_ID}" \
        -H "X-Auth-Token: ${TOKEN}" \
        | grep -oP '"status"\s*:\s*"\K[^"]+' | head -1)
    case "$STATUS" in
        active)  echo "Image ${IMAGE_ID} active. Done."; exit 0 ;;
        saving|queued|importing) sleep 3 ;;
        *) echo "Error: unexpected status '${STATUS}'" >&2; exit 1 ;;
    esac
done

echo "Warning: image did not reach active in time; last: ${STATUS}" >&2
exit 1
