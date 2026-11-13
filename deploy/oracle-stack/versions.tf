# Muse always-on helper: an Oracle Cloud Resource Manager stack.
#
# One Always Free ARM instance (2 OCPU, 12 GB), a network that lets nothing in,
# and a private bucket for encrypted backups. deploy/oracle-stack/README.md
# explains every resource.
#
# NOT YET APPLIED to a real tenancy. Read README.md, "Not tested yet".

terraform {
  # Lifecycle preconditions (used in compute.tf) need Terraform 1.2 or later.
  required_version = ">= 1.2.0"

  required_providers {
    oci = {
      source = "oracle/oci"
      # Exact pin. Released 2026-11-05 (github.com/oracle/terraform-provider-oci/releases).
      version = "= 9.12.1"
    }
  }
}

# Resource Manager supplies the credentials; the region is the only field it
# needs (docs: "Terraform Configurations for Resource Manager").
provider "oci" {
  region = var.region
}
