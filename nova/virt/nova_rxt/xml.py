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

"""Libvirt domain-XML helpers for Xen emulation.

Isolating lxml here keeps the driver module itself free of XML parsing
machinery and makes the transform trivially unit-testable without any
Nova imports.
"""

from lxml import etree


QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"
QEMU_PREFIX = "qemu"


def inject_qemu_commandline(xml_str, qemu_args):
    """Append ``<qemu:commandline>`` to a libvirt ``<domain>`` XML string.

    Every value in ``qemu_args`` becomes one ``<qemu:arg value='...'/>``
    child in the order given. The ``xmlns:qemu`` declaration is added to
    the root ``<domain>`` element if not already present.

    :param xml_str: serialized domain XML (``str`` or ``bytes``).
    :param qemu_args: iterable of strings. Typical value is the tuple
        registered as ``[xen_emulation] qemu_args`` in nova.conf.
    :returns: unicode ``str`` with the commandline appended.
    """
    data = xml_str if isinstance(xml_str, bytes) else xml_str.encode("utf-8")
    root = etree.fromstring(data)

    if root.nsmap.get(QEMU_PREFIX) != QEMU_NS:
        root = _clone_with_namespace(root, QEMU_PREFIX, QEMU_NS)

    cmdline = etree.SubElement(root, "{%s}commandline" % QEMU_NS)
    for value in qemu_args:
        arg = etree.SubElement(cmdline, "{%s}arg" % QEMU_NS)
        arg.set("value", value)

    return etree.tostring(root, encoding="unicode")


def _clone_with_namespace(root, prefix, uri):
    # lxml does not permit mutating an Element's nsmap after creation;
    # adding an xmlns declaration at the root requires re-building the
    # element with an extended nsmap and re-parenting its children.
    nsmap = dict(root.nsmap)
    nsmap[prefix] = uri
    new_root = etree.Element(root.tag, attrib=dict(root.attrib), nsmap=nsmap)
    new_root.text = root.text
    for child in list(root):
        new_root.append(child)
    return new_root
