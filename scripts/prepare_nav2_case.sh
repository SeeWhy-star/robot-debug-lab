#!/bin/bash
# No sudo or host package changes. Dependencies live in an ignored root filesystem.
set -euo pipefail
RDL_CASE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RDL_CASE_DIR="$RDL_CASE_ROOT/.runtime/nav2-5710"
command -v bwrap >/dev/null || { echo '需要 bubblewrap (bwrap) 和可用的非特权用户命名空间'; exit 1; }
[[ "$(uname -m)" == x86_64 ]] || { echo '此案例环境仅支持 Linux x86_64'; exit 1; }
mkdir -p "$RDL_CASE_DIR"
exec 9>"$RDL_CASE_DIR/prepare.lock"
flock -n 9 || { echo '案例环境正在准备'; exit 1; }
cd "$RDL_CASE_DIR"
fetch() {
  local url="$1" dest="$2" digest="$3"
  if [[ ! -f "$dest" ]]; then curl -fL --retry 2 "$url" -o "$dest.part"; mv "$dest.part" "$dest"; fi
  echo "$digest  $dest" | sha256sum -c -
}
fetch https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/ubuntu-base-24.04.3-base-amd64.tar.gz ubuntu-base.tar.gz 6bc2cde3930ad088b3bb46fa45279e96d25bc3810f209850ecbe4722711874f9
fetch https://codeload.github.com/ros-navigation/navigation2/tar.gz/6794f00fb316efc2e9ede468d913021a481f57a1 nav2.tar.gz c9b68723cfd6bb9d5f6b4fcdb1e3a0718cfdc4f8f5a35ebe6a48877f73075c2b
fetch https://raw.githubusercontent.com/ros/rosdistro/master/ros.key ros.key 4a91c49af0d6f0016108b93698782b596c27ccd836937e18e0e36c3347dc602f
if [[ ! -d rootfs/etc ]]; then mkdir -p rootfs; tar --no-same-owner -xzf ubuntu-base.tar.gz -C rootfs; fi
mkdir -p upstream rootfs/work rootfs/harness
tar --no-same-owner --strip-components=1 -xzf nav2.tar.gz -C upstream
cp ros.key rootfs/usr/share/keyrings/ros-archive-keyring.gpg
cat > rootfs/etc/apt/sources.list.d/ros2.list <<'EOF'
deb [arch=amd64 signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu noble main
EOF
echo 'APT::Sandbox::User "root";' > rootfs/etc/apt/apt.conf.d/99rootless
cat > rootfs/usr/sbin/policy-rc.d <<'EOF'
#!/bin/sh
exit 101
EOF
chmod +x rootfs/usr/sbin/policy-rc.d
if [[ ! -f ready ]]; then
  echo '正在安装隔离环境依赖，日志：.runtime/nav2-5710/install.log'
  bwrap --die-with-parent --unshare-user --unshare-pid --unshare-ipc --uid 0 --gid 0 \
    --bind "$RDL_CASE_DIR/rootfs" / --dev /dev --proc /proc \
    --ro-bind /etc/resolv.conf /etc/resolv.conf --clearenv \
    --setenv PATH /usr/bin:/bin --setenv HOME /root --setenv LC_ALL C \
    --setenv DEBIAN_FRONTEND noninteractive /bin/bash -ec \
    'apt-get update; apt-get install -y --no-install-recommends build-essential cmake ros-jazzy-nav2-costmap-2d; dpkg-query -W > /work/packages.tsv' \
    > install.log 2>&1
  cp rootfs/work/packages.tsv packages.tsv
  touch ready
fi
echo '案例环境已就绪。运行：.venv/bin/python scripts/nav2_case.py reproduce'
