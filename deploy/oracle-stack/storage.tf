# A private bucket for the helper's encrypted backups.
#
# It exists only when backups are on AND the helper can be given its own
# identity (iam.tf). Without that identity nothing on the VM could write to a
# bucket without a stored key, so no bucket is made and backups stay on the
# helper's disk (the helper's status says so in plain words).
#
# Free allowance: 20 GB of Object Storage in total and 50,000 requests a month.
# A backup is a few megabytes; backup_keep of them stay.

locals {
  bucket_enabled = var.enable_backups && var.create_identity_resources
  bucket_name    = local.bucket_enabled ? "muse-backups-${local.suffix}" : ""
}

data "oci_objectstorage_namespace" "this" {
  compartment_id = var.compartment_ocid
}

resource "oci_objectstorage_bucket" "backups" {
  count = local.bucket_enabled ? 1 : 0

  compartment_id        = var.compartment_ocid
  namespace             = data.oci_objectstorage_namespace.this.namespace
  name                  = local.bucket_name
  access_type           = "NoPublicAccess"
  storage_tier          = "Standard"
  versioning            = "Disabled"
  auto_tiering          = "Disabled"
  object_events_enabled = false

  freeform_tags = {
    "muse" = "backups"
  }
}

# A second line behind the helper's own pruning: the bucket deletes backups
# older than backup_expire_days and clears unfinished uploads. Oracle runs
# lifecycle rules only when its Object Storage service is allowed to (the
# service statement in iam.tf), hence the dependency.
resource "oci_objectstorage_object_lifecycle_policy" "backups" {
  count = local.bucket_enabled ? 1 : 0

  namespace = data.oci_objectstorage_namespace.this.namespace
  bucket    = oci_objectstorage_bucket.backups[0].name

  rules {
    name        = "expire-old-backups"
    action      = "DELETE"
    target      = "objects"
    is_enabled  = true
    time_amount = var.backup_expire_days
    time_unit   = "DAYS"

    object_name_filter {
      inclusion_prefixes = ["backups/"]
    }
  }

  rules {
    name        = "clear-unfinished-uploads"
    action      = "ABORT"
    target      = "multipart-uploads"
    is_enabled  = true
    time_amount = 2
    time_unit   = "DAYS"
  }

  depends_on = [oci_identity_policy.backups]
}
