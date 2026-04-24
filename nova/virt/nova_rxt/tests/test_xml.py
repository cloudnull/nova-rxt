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

import unittest

from lxml import etree

from nova.virt.nova_rxt import xml as xmlinject


class InjectQemuCommandlineTest(unittest.TestCase):

    def _parse(self, xml_str):
        return etree.fromstring(xml_str.encode("utf-8"))

    def test_qemu_namespace_shape(self):
        xml = "<domain type='kvm'><name>foo</name></domain>"
        result = xmlinject.inject_qemu_commandline(xml, ["-a", "b"])
        # Namespace declaration must land on <domain>, not on a child.
        self.assertIn(
            'xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0"',
            result,
        )
        root = self._parse(result)
        self.assertEqual(root.nsmap.get(xmlinject.QEMU_PREFIX), xmlinject.QEMU_NS)

    def test_commandline_is_last_child(self):
        xml = "<domain type='kvm'>" "<name>foo</name>" "<uuid>12345</uuid>" "</domain>"
        result = xmlinject.inject_qemu_commandline(xml, ["-a", "b"])
        root = self._parse(result)
        last = root[-1]
        self.assertEqual(etree.QName(last).localname, "commandline")
        self.assertEqual(etree.QName(last).namespace, xmlinject.QEMU_NS)

    def test_args_serialized_in_order(self):
        xml = "<domain type='kvm'><name>f</name></domain>"
        args = ["-accel", "kvm,xen-version=0x40011,kernel-irqchip=split"]
        result = xmlinject.inject_qemu_commandline(xml, args)
        root = self._parse(result)
        cmdline = root[-1]
        values = [child.get("value") for child in cmdline]
        self.assertEqual(values, args)

    def test_existing_children_preserved(self):
        xml = (
            "<domain type='kvm'>"
            "<name>foo</name>"
            "<uuid>12345</uuid>"
            "<devices><disk type='file'/></devices>"
            "</domain>"
        )
        result = xmlinject.inject_qemu_commandline(xml, ["-x"])
        root = self._parse(result)
        tags = [etree.QName(c).localname for c in root]
        self.assertEqual(tags, ["name", "uuid", "devices", "commandline"])

    def test_root_attributes_preserved(self):
        xml = "<domain type='kvm' id='42'><name>f</name></domain>"
        result = xmlinject.inject_qemu_commandline(xml, ["-x"])
        root = self._parse(result)
        self.assertEqual(root.get("type"), "kvm")
        self.assertEqual(root.get("id"), "42")

    def test_namespace_already_declared(self):
        xml = (
            "<domain type='kvm' "
            "xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>"
            "<name>foo</name></domain>"
        )
        result = xmlinject.inject_qemu_commandline(xml, ["-a"])
        root = self._parse(result)
        self.assertEqual(root.nsmap.get(xmlinject.QEMU_PREFIX), xmlinject.QEMU_NS)
        # Must not duplicate the declaration.
        self.assertEqual(
            result.count("xmlns:qemu="),
            1,
            msg=("xmlns:qemu declared more than once: %s" % result),
        )

    def test_empty_args_produces_empty_commandline(self):
        xml = "<domain type='kvm'><name>f</name></domain>"
        result = xmlinject.inject_qemu_commandline(xml, [])
        root = self._parse(result)
        cmdline = root[-1]
        self.assertEqual(etree.QName(cmdline).localname, "commandline")
        self.assertEqual(len(cmdline), 0)

    def test_bytes_input_accepted(self):
        xml = b"<domain type='kvm'><name>f</name></domain>"
        result = xmlinject.inject_qemu_commandline(xml, ["-a"])
        self.assertIn("<qemu:arg", result)

    def test_text_nodes_preserved(self):
        # Some serializers produce leading text inside <domain> — make
        # sure we don't lose it.
        xml = "<domain type='kvm'>\n  <name>foo</name>\n</domain>"
        result = xmlinject.inject_qemu_commandline(xml, ["-a"])
        # The original name element and whitespace survive alongside the
        # injected commandline.
        self.assertIn("<name>foo</name>", result)
        self.assertIn("<qemu:commandline>", result)


if __name__ == "__main__":
    unittest.main()
