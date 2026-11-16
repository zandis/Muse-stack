# The helper itself: one Ampere A1 Flex instance, Ubuntu 24.04 (aarch64).

locals {
  shape = "VM.Standard.A1.Flex"

  # The release to install: a variable when given, otherwise what build-zip.sh baked in.
  release_version = trimprefix(var.muse_version != "" ? var.muse_version : local.pinned_release.version, "v")
  release_url     = var.muse_release_url != "" ? var.muse_release_url : local.pinned_release.url
  release_sha256  = var.muse_release_sha256 != "" ? var.muse_release_sha256 : local.pinned_release.sha256

  image_id = var.image_ocid != "" ? var.image_ocid : data.oci_core_images.ubuntu.images[0].id

  # The idle guard: on for an Always Free account, off for a paid one, unless set to on or off.
  idle_guard_on = var.idle_guard == "auto" ? var.account_type == "always_free" : var.idle_guard == "on"

  # Everything first boot needs, none of it secret: the instance's user data can
  # be read by any process on the VM and is kept in the stack's state.
  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    muse_version        = local.release_version
    release_url         = local.release_url
    release_sha256      = local.release_sha256
    time_zone           = var.time_zone
    users_block         = local.tester_ssh ? "users:\n  - default" : "users: []"
    tester_ssh          = local.tester_ssh ? "yes" : "no"
    backup              = var.enable_backups ? "on" : "off"
    backup_bucket       = local.bucket_name
    backup_namespace    = local.bucket_enabled ? data.oci_objectstorage_namespace.this.namespace : ""
    backup_keep         = var.backup_keep
    account_type        = var.account_type
    idle_guard          = local.idle_guard_on ? "on" : "off"
    ocpus               = var.ocpus
    memory_gb           = var.memory_in_gbs
    update_manifest_url = var.update_manifest_url
    update_public_key   = var.update_public_key
  })
}

# The newest Ubuntu 24.04 image Oracle publishes that runs on this shape. The
# shape filter is what selects the aarch64 build.
data "oci_core_images" "ubuntu" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "24.04"
  shape                    = local.shape
  state                    = "AVAILABLE"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

resource "oci_core_instance" "muse" {
  compartment_id      = var.compartment_ocid
  availability_domain = var.availability_domain
  display_name        = "muse-helper"
  shape               = local.shape

  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_in_gbs
  }

  source_details {
    source_type             = "image"
    source_id               = local.image_id
    boot_volume_size_in_gbs = var.boot_volume_size_in_gbs
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.muse.id
    display_name     = "muse-helper"
    hostname_label   = "muse-helper"
    assign_public_ip = true
  }

  # user_data is gzip-compressed, then base64: cloud-init accepts both.
  # No SSH key is passed unless an early tester gave a key AND a source address.
  metadata = merge(
    { user_data = base64gzip(local.user_data) },
    local.tester_ssh ? { ssh_authorized_keys = var.ssh_public_key } : {}
  )

  # Only the newer metadata service, which needs a header a stray request lacks.
  instance_options {
    are_legacy_imds_endpoints_disabled = true
  }

  # The monitoring plugin reports the CPU, memory and network figures Oracle's
  # idle rule is measured on; they show in the Console under the instance's Metrics.
  agent_config {
    is_monitoring_disabled = false
    is_management_disabled = false

    plugins_config {
      name          = "Compute Instance Monitoring"
      desired_state = "ENABLED"
    }
  }

  availability_config {
    recovery_action = "RESTORE_INSTANCE"
  }

  is_pv_encryption_in_transit_enabled = true
  preserve_boot_volume                = false

  freeform_tags = {
    "muse" = "always-on-helper"
  }

  lifecycle {
    # First boot runs once. A newer image or changed settings must not rebuild a
    # running helper and its disk on a later apply.
    ignore_changes = [source_details[0].source_id, metadata]

    precondition {
      condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+([.+-][0-9A-Za-z.]+)?$", local.release_version))
      error_message = "No fixed Muse version is set. This stack must be built with build-zip.sh, which pins a release; a branch name or 'latest' is refused."
    }

    precondition {
      condition     = can(regex("^[0-9a-f]{64}$", local.release_sha256))
      error_message = "No SHA-256 is set for the Muse release. First boot only installs a release that matches a pinned checksum."
    }

    precondition {
      condition     = can(regex("^https://[^\\s'\"]+$", local.release_url))
      error_message = "The Muse release address must start with https://."
    }

    precondition {
      condition     = (var.ssh_public_key == "") == (var.ssh_source_cidr == "")
      error_message = "For tester SSH give both a public key and the one address it is allowed from, or neither."
    }
  }
}
