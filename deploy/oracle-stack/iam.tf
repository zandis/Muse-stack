# Optional: the helper's own identity, so it can write backups with NO stored key.
#
# A dynamic group whose only member is this one instance, and a policy that
# lets that group read the backup bucket and manage the objects in it. Nothing
# else: it cannot see other buckets, other resources, or any identity setting.
#
# Dynamic groups live at tenancy level, so creating one needs rights there. The
# person who created a free account is in the Administrators group and has
# them. Someone with rights in one compartment only does not: set
# create_identity_resources = false, and backups stay on the helper's disk.
#
# Identity resources can only be created in the tenancy's home region, which is
# also where Always Free compute must be.

locals {
  identity_enabled   = local.bucket_enabled
  dynamic_group_name = "muse-helper-${local.suffix}"
  # A policy names the root compartment as "tenancy" and any other by its OCID.
  policy_scope = var.compartment_ocid == var.tenancy_ocid ? "tenancy" : "compartment id ${var.compartment_ocid}"
}

resource "oci_identity_dynamic_group" "helper" {
  count = local.identity_enabled ? 1 : 0

  compartment_id = var.tenancy_ocid
  name           = local.dynamic_group_name
  description    = "The Muse always-on helper instance, and nothing else"
  matching_rule  = "instance.id = '${oci_core_instance.muse.id}'"
}

resource "oci_identity_policy" "backups" {
  count = local.identity_enabled ? 1 : 0

  compartment_id = var.tenancy_ocid
  name           = "muse-helper-backups-${local.suffix}"
  description    = "Lets the Muse helper instance keep encrypted backups in its own bucket"

  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.helper[0].name} to read buckets in ${local.policy_scope} where target.bucket.name='${local.bucket_name}'",
    "Allow dynamic-group ${oci_identity_dynamic_group.helper[0].name} to manage objects in ${local.policy_scope} where target.bucket.name='${local.bucket_name}'",
    # Oracle's documented statement that lets the Object Storage service run
    # lifecycle rules (delete old backups), limited to the permissions it lists.
    "Allow service objectstorage-${var.region} to manage object-family in ${local.policy_scope} where any {request.permission='BUCKET_INSPECT', request.permission='BUCKET_READ', request.permission='OBJECT_INSPECT', request.permission='OBJECT_UPDATE_TIER', request.permission='OBJECT_DELETE', request.permission='OBJECT_VERSION_DELETE'}",
  ]
}
