# The network: outbound only.
#
# A small VCN, one public subnet, an internet gateway for outbound traffic, and
# a security list with NO inbound rule. The instance has a public address only
# because an internet gateway needs one to carry outbound traffic; nothing can
# start a connection to it. Replies to connections the helper starts come back
# because the outbound rules are stateful (Oracle's docs, "Security Rules").
#
# Why not a private subnet behind a NAT gateway: Oracle's Always Free page does
# not list NAT gateways, so it could not be confirmed as free. A public subnet
# with an empty inbound list is free and has the same effect.

locals {
  suffix     = var.name_suffix != "" ? var.name_suffix : substr(sha256(var.compartment_ocid), 0, 6)
  tester_ssh = var.ssh_public_key != "" && var.ssh_source_cidr != ""
}

resource "oci_core_vcn" "muse" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = ["10.77.0.0/24"]
  display_name   = "muse-helper"
  dns_label      = "muse"
}

resource "oci_core_internet_gateway" "muse" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.muse.id
  display_name   = "muse-helper-outbound"
  enabled        = true
}

resource "oci_core_route_table" "muse" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.muse.id
  display_name   = "muse-helper"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.muse.id
    description       = "Outbound to the internet"
  }
}

# The only security list the subnet uses. Outbound: HTTPS, and HTTP for
# Ubuntu's package mirrors (packages are signature-checked by apt). Inbound:
# nothing, unless an early tester supplied BOTH a key and one source address.
resource "oci_core_security_list" "muse" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.muse.id
  display_name   = "muse-helper-no-inbound"

  egress_security_rules {
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    protocol         = "6"
    stateless        = false
    description      = "HTTPS out: AI providers, the Muse release, Oracle's own services"

    tcp_options {
      min = 443
      max = 443
    }
  }

  egress_security_rules {
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    protocol         = "6"
    stateless        = false
    description      = "HTTP out: Ubuntu package mirrors"

    tcp_options {
      min = 80
      max = 80
    }
  }

  # Early testers only; empty by default. variables.tf refuses 0.0.0.0/0.
  dynamic "ingress_security_rules" {
    for_each = local.tester_ssh ? [var.ssh_source_cidr] : []

    content {
      source      = ingress_security_rules.value
      source_type = "CIDR_BLOCK"
      protocol    = "6"
      stateless   = false
      description = "Early tester SSH from one address"

      tcp_options {
        min = 22
        max = 22
      }
    }
  }
}

# Every VCN is born with a default security list that allows SSH in. The subnet
# below does not use it, and this empties it so that it allows nothing if it is
# ever attached by mistake.
resource "oci_core_default_security_list" "locked" {
  manage_default_resource_id = oci_core_vcn.muse.default_security_list_id
  display_name               = "muse-helper-default-emptied"
}

resource "oci_core_subnet" "muse" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.muse.id
  cidr_block                 = "10.77.0.0/28"
  display_name               = "muse-helper"
  dns_label                  = "helper"
  route_table_id             = oci_core_route_table.muse.id
  security_list_ids          = [oci_core_security_list.muse.id]
  prohibit_public_ip_on_vnic = false
}
