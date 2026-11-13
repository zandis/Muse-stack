# Filled in by Resource Manager from the signed-in session.

variable "tenancy_ocid" {
  description = "The tenancy. Resource Manager fills this in."
  type        = string
}

variable "region" {
  description = "The region to create everything in. Always Free compute must be in the tenancy's home region."
  type        = string
}

variable "compartment_ocid" {
  description = "The compartment that will hold the helper."
  type        = string
}

# The one thing the person chooses.

variable "availability_domain" {
  description = "Where in the region the helper runs. If Oracle answers 'out of host capacity', pick another one and apply again."
  type        = string
}

variable "show_advanced" {
  description = "Only shows or hides the advanced part of the form. It changes nothing that is created."
  type        = bool
  default     = false
}

# Size. The defaults are the whole Always Free ARM allowance and nothing more.

variable "ocpus" {
  description = "Processors. Always Free covers 2 in total."
  type        = number
  default     = 2

  validation {
    condition     = var.ocpus >= 1 && var.ocpus <= 2 && floor(var.ocpus) == var.ocpus
    error_message = "Choose 1 or 2 processors; more than 2 is not free."
  }
}

variable "memory_in_gbs" {
  description = "Memory in GB. Always Free covers 12 in total."
  type        = number
  default     = 12

  validation {
    condition     = var.memory_in_gbs >= 6 && var.memory_in_gbs <= 12
    error_message = "Choose between 6 and 12 GB; more than 12 is not free."
  }
}

variable "boot_volume_size_in_gbs" {
  description = "Disk size in GB. Always Free covers 200 GB of disk in total across the tenancy."
  type        = number
  default     = 50

  validation {
    condition     = var.boot_volume_size_in_gbs >= 50 && var.boot_volume_size_in_gbs <= 200
    error_message = "Choose between 50 and 200 GB; more than 200 is not free."
  }
}

variable "time_zone" {
  description = "Time zone for schedules and the nightly maintenance window."
  type        = string
  default     = "Asia/Taipei"

  validation {
    condition     = can(regex("^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}$", var.time_zone))
    error_message = "Use a zone name such as Asia/Taipei."
  }
}

# Backups.

variable "enable_backups" {
  description = "Make a daily encrypted backup."
  type        = bool
  default     = true
}

variable "create_identity_resources" {
  description = "Let the helper write backups to its own bucket without any stored key (creates a dynamic group and a policy; needs rights at tenancy level, which the person who created a free account has). When off, no bucket is created and backups stay on the helper's own disk."
  type        = bool
  default     = true
}

variable "backup_keep" {
  description = "How many daily backups to keep in the bucket."
  type        = number
  default     = 14

  validation {
    condition     = var.backup_keep >= 1 && var.backup_keep <= 60 && floor(var.backup_keep) == var.backup_keep
    error_message = "Keep between 1 and 60 backups."
  }
}

variable "backup_expire_days" {
  description = "The bucket itself deletes any backup older than this many days, as a second line behind backup_keep."
  type        = number
  default     = 35

  validation {
    condition     = var.backup_expire_days >= 7 && var.backup_expire_days <= 365 && floor(var.backup_expire_days) == var.backup_expire_days
    error_message = "Choose between 7 and 365 days."
  }
}

# Idle guard.

variable "idle_guard" {
  description = "Keep memory use above the level at which Oracle may reclaim an idle Always Free instance. See README.md, 'Idle guard'."
  type        = bool
  default     = true
}

# Early testers only. Leave both empty and no SSH access exists at all.

variable "ssh_public_key" {
  description = "Early testers only: an SSH public key. Empty (the default) means no SSH access is configured at all."
  type        = string
  default     = ""

  validation {
    condition     = var.ssh_public_key == "" || can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/=]+( [^\\n\\r]*)?$", var.ssh_public_key))
    error_message = "Paste one public key line, for example: ssh-ed25519 AAAA... name"
  }
}

variable "ssh_source_cidr" {
  description = "Early testers only: the one address SSH is allowed from, such as 203.0.113.7/32. Needed together with ssh_public_key. 'Anywhere' is refused."
  type        = string
  default     = ""

  validation {
    condition     = var.ssh_source_cidr == "" || (can(cidrhost(var.ssh_source_cidr, 0)) && !can(regex("/([0-9]|1[0-9]|2[0-3])$", var.ssh_source_cidr)))
    error_message = "Give one address or a small range (/24 to /32), never 0.0.0.0/0."
  }
}

# Normally left alone.

variable "image_ocid" {
  description = "A specific Ubuntu 24.04 aarch64 image. Empty means the newest one Oracle publishes for this shape."
  type        = string
  default     = ""
}

variable "name_suffix" {
  description = "A short suffix for names that must be unique in the tenancy. Empty means one derived from the compartment."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^[a-z0-9]{0,12}$", var.name_suffix))
    error_message = "Use up to 12 lower-case letters and digits."
  }
}

# The Muse release the helper installs. build-zip.sh bakes these into
# release_pin.tf; set them here only to override that.

variable "muse_version" {
  description = "A fixed Muse version such as 0.2.0. Never a branch or 'latest'."
  type        = string
  default     = ""
}

variable "muse_release_url" {
  description = "https address of that release's tarball."
  type        = string
  default     = ""
}

variable "muse_release_sha256" {
  description = "SHA-256 of that tarball. First boot refuses a download that does not match."
  type        = string
  default     = ""
}

# Muse's own updates. Off until both are set.

variable "update_manifest_url" {
  description = "https address of the signed release manifest. Empty means Muse updates are off (operating-system security updates stay on)."
  type        = string
  default     = ""

  validation {
    condition     = var.update_manifest_url == "" || can(regex("^https://[^\\s'\"]+$", var.update_manifest_url))
    error_message = "The manifest address must start with https://."
  }
}

variable "update_public_key" {
  description = "Base64 of the Ed25519 public key the manifest is signed with."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^[A-Za-z0-9+/=]{0,64}$", var.update_public_key))
    error_message = "Paste the base64 public key only."
  }
}
