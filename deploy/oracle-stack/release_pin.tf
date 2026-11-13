# The Muse release this stack installs.
#
# In the repository these are empty on purpose, and a plan fails with a clear
# message. build-zip.sh writes the real values into the copy that goes in the
# stack zip, so the person deploying has nothing to fill in:
#
#   deploy/oracle-stack/build-zip.sh --version 0.2.0 \
#       --release-url https://.../muse-0.2.0.tar.gz --release-sha256 <64 hex>

locals {
  pinned_release = {
    version = ""
    url     = ""
    sha256  = ""
  }
}
