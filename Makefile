# ChimeraStick - Global Build System Orchestrator
# Assumes a Linux environment (Ubuntu/Debian or Alpine) with standard build tools.

SHELL := /bin/bash
ALPINE_VER := 3.19
ARCH := x86_64
NETBOOT_URL := https://dl-cdn.alpinelinux.org/alpine/v$(ALPINE_VER)/releases/$(ARCH)/netboot

.PHONY: all clean payload windows-installer linux-installer directories

all: payload windows-installer linux-installer
	@echo "=================================================="
	@echo "  ChimeraStick production build complete!"
	@echo "  Deliverables:"
	@echo "    - ChimeraStick2Disk.exe  (Windows Installer)"
	@echo "    - ChimeraStick2Disk.sh   (Linux Installer)"
	@echo "=================================================="

directories:
	@mkdir -p payload/boot payload/efi/EFI/BOOT build

payload: directories build/rootfs.squashfs
	@echo "Downloading Alpine LTS netboot files..."
	@wget -q -N $(NETBOOT_URL)/vmlinuz-lts -P payload/boot/
	@wget -q -N $(NETBOOT_URL)/initramfs-lts -P payload/boot/
	@wget -q -N $(NETBOOT_URL)/modloop-lts -P payload/boot/
	
	@echo "Downloading Debian signed shim and GRUB packages..."
	@mkdir -p build/signed
	@wget -q -O build/signed/shim-signed.deb http://ftp.us.debian.org/debian/pool/main/s/shim-signed/shim-signed_1.44~1+deb12u1+15.8-1~deb12u1_amd64.deb
	@wget -q -O build/signed/grub-signed.deb http://ftp.us.debian.org/debian/pool/main/g/grub-efi-amd64-signed/grub-efi-amd64-signed_1+2.06+13+deb12u2_amd64.deb
	
	@echo "Extracting signed EFI binaries..."
	@cd build/signed && ar x shim-signed.deb && tar -xf data.tar.* ./usr/lib/shim/shimx64.efi.signed
	@cd build/signed && ar x grub-signed.deb && tar -xf data.tar.* ./usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed
	
	@cp build/signed/usr/lib/shim/shimx64.efi.signed payload/efi/EFI/BOOT/bootx64.efi
	@cp build/signed/usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed payload/efi/EFI/BOOT/grubx64.efi
	@cp src/build/early-grub.cfg payload/efi/EFI/BOOT/grub.cfg
	@rm -rf build/signed

build/rootfs.squashfs:
	@echo "Executing Alpine RootFS creation script..."
	@sudo ./src/build/build_rootfs.sh

windows-installer:
	@echo "Building Windows Go installer..."
	@rm -rf src/installer/windows/payload
	@mkdir -p src/installer/windows/payload
	@cp -r payload/* src/installer/windows/payload/
	@cd src/installer/windows && GOOS=windows GOARCH=amd64 go build -ldflags="-s -w" -o ../../../ChimeraStick2Disk.exe
	@rm -rf src/installer/windows/payload
	@echo "Windows installer successfully compiled."

linux-installer:
	@echo "Assembling self-extracting Linux installer..."
	@cd payload && tar -czf ../build/payload.tar.gz .
	@cp src/installer/linux/ChimeraStick2Disk.sh ChimeraStick2Disk.sh
	@echo "" >> ChimeraStick2Disk.sh
	@base64 build/payload.tar.gz >> ChimeraStick2Disk.sh
	@chmod +x ChimeraStick2Disk.sh
	@rm -f build/payload.tar.gz
	@echo "Linux installer successfully assembled."

clean:
	@echo "Cleaning up temporary directories..."
	@sudo rm -rf build payload/rootfs.squashfs payload/boot/vmlinuz-lts payload/boot/initramfs-lts payload/boot/modloop-lts payload/efi/EFI
	@rm -f ChimeraStick2Disk.exe ChimeraStick2Disk.sh
	@rm -rf src/installer/windows/payload
