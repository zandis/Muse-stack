# What the stack reports when it finishes. Nothing here is a credential: the
# helper's keys are made on the helper and never leave it through Oracle.

output "instance_ocid" {
  description = "The helper's identifier in Oracle Cloud."
  value       = oci_core_instance.muse.id
}

output "instance_name" {
  description = "The helper's name in the Console."
  value       = oci_core_instance.muse.display_name
}

output "muse_version" {
  description = "The Muse release first boot installs."
  value       = local.release_version
}

output "size" {
  description = "Processors and memory, against the Always Free allowance of 2 and 12 GB."
  value       = "${var.ocpus} OCPU, ${var.memory_in_gbs} GB"
}

output "backup_bucket" {
  description = "Where encrypted backups go."
  value       = local.bucket_enabled ? local.bucket_name : "none: backups stay on the helper's own disk"
}

output "inbound_access" {
  description = "What can reach the helper from outside."
  value       = local.tester_ssh ? "SSH from ${var.ssh_source_cidr} only (early tester)" : "nothing"
}

output "tester_ssh_address" {
  description = "Early testers only: the address to SSH to. Empty when no SSH access was configured."
  value       = local.tester_ssh ? oci_core_instance.muse.public_ip : ""
}

output "next_step" {
  description = "What to do now."
  value       = "The helper sets itself up in about ten minutes; nothing more is needed here. Pairing it from the Muse app arrives with a later release. Until then, see the README section 'Reaching the helper'."
}
