package main

import (
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/sys/windows"
)

//go:embed all:payload
var payload embed.FS

type usbDrive struct {
	name     string
	deviceId string // In Windows, this is the disk number (e.g., "1")
	sizeGB   int
}

func main() {
	// 1. Force Administrator privileges
	if !amAdmin() {
		fmt.Println("Administrator privileges are required to partition physical USB disks.")
		fmt.Println("Attempting to launch installer with elevation...")
		runMeElevated()
		return
	}

	fmt.Println("==================================================")
	fmt.Println("         ChimeraStick USB Installer (Windows)     ")
	fmt.Println("==================================================")
	fmt.Println("Detecting connected USB/External drives...")

	drives, err := listRemovableDrives()
	if err != nil {
		fmt.Printf("Error scanning drives: %v\n", err)
		cleanupAndExit(5)
	}

	if len(drives) == 0 {
		fmt.Println("\nNo removable external storage drives found.")
		fmt.Println("Please insert a USB drive and run the installer again.")
		cleanupAndExit(5)
	}

	fmt.Println("\nAvailable external drives:")
	for i, d := range drives {
		fmt.Printf("[%d] Disk %s: %s (%d GB)\n", i+1, d.deviceId, d.name, d.sizeGB)
	}

	var choice int
	for {
		fmt.Printf("\nSelect drive number [1-%d]: ", len(drives))
		_, err := fmt.Scan(&choice)
		if err == nil && choice >= 1 && choice <= len(drives) {
			break
		}
		// Clear stdin buffer
		var dump string
		fmt.Scanln(&dump)
		fmt.Println("Invalid selection. Please enter a valid index.")
	}

	target := drives[choice-1]

	fmt.Println("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
	fmt.Printf(" WARNING: ALL DATA ON DISK %s (%s) WILL BE DESTROYED!\n", target.deviceId, target.name)
	fmt.Println(" This includes all partitions, files, and operating systems.")
	fmt.Println("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
	
	fmt.Print("\nAre you absolutely sure you want to proceed? (Type 'YES' to confirm): ")
	var confirm string
	fmt.Scan(&confirm)
	if confirm != "YES" {
		fmt.Println("Installation cancelled.")
		cleanupAndExit(0)
	}

	// 2. Select 3 unused drive letters to avoid conflicts on host system
	fmt.Println("\nAllocating temporary drive letters...")
	letters, err := getUnusedLetters(3)
	if err != nil {
		fmt.Printf("Failed to allocate drive letters: %v\n", err)
		cleanupAndExit(1)
	}
	efiLetter := letters[0]
	bootLetter := letters[1]
	dataLetter := letters[2]
	fmt.Printf("Assigned temporary mount points: EFI (%s:), BOOT (%s:), DATA (%s:)\n", efiLetter, bootLetter, dataLetter)

	// 3. Partition and format drive
	fmt.Println("\nPartitioning and formatting USB drive via diskpart...")
	fmt.Println("This may take up to a minute...")
	err = partitionDrive(target.deviceId, efiLetter, bootLetter, dataLetter)
	if err != nil {
		fmt.Printf("Error partitioning drive: %v\n", err)
		cleanupAndExit(2)
	}

	// 4. Extract files to partitions
	fmt.Println("\nWriting operating system files to USB...")
	
	// EFI files to Partition 1
	efiDest := efiLetter + ":\\"
	fmt.Printf("-> Writing EFI system bootloader to %s...\n", efiDest)
	err = extractEmbedDir("payload/efi", efiDest)
	if err != nil {
		fmt.Printf("Failed to write EFI files: %v\n", err)
		cleanupAndExit(3)
	}

	// Boot & OS files to Partition 2 (FAT32 labeled "BOOT" matches root=LABEL=BOOT boot criteria)
	bootDest := bootLetter + ":\\"
	fmt.Printf("-> Writing Linux kernel, initramfs and root OS image to %s...\n", bootDest)
	err = extractEmbedDir("payload/boot", bootDest)
	if err != nil {
		fmt.Printf("Failed to write kernel files: %v\n", err)
		cleanupAndExit(3)
	}
	err = extractEmbedFile("payload/rootfs.squashfs", filepath.Join(bootDest, "rootfs.squashfs"))
	if err != nil {
		fmt.Printf("Failed to write root filesystem squashfs: %v\n", err)
		cleanupAndExit(3)
	}

	// Structure config/shared directories on Partition 3
	dataDest := dataLetter + ":\\"
	fmt.Printf("-> Initializing storage layout on %s...\n", dataDest)
	os.MkdirAll(filepath.Join(dataDest, "config"), 0755)
	os.MkdirAll(filepath.Join(dataDest, "shared"), 0755)

	// 5. Clean up temporary drive letters
	fmt.Println("\nCleaning up mount points...")
	removeMountLetters(efiLetter, bootLetter, dataLetter)

	fmt.Println("\n==================================================")
	fmt.Println("     ChimeraStick Installation Completed!         ")
	fmt.Println("==================================================")
	fmt.Println("Your USB pendrive is now a secure bootable server.")
	fmt.Println("1. Insert the USB into any PC.")
	fmt.Println("2. Restart the PC and enter the Boot Menu (F12/F11/F8/Esc).")
	fmt.Println("3. Select the UEFI or Legacy USB option to start ChimeraStick.")
	
	cleanupAndExit(0)
}

func amAdmin() bool {
	return windows.IsUserAnAdmin()
}

func runMeElevated() {
	verb := "runas"
	exe, err := os.Executable()
	if err != nil {
		return
	}
	cwd, _ := os.Getwd()
	args := strings.Join(os.Args[1:], " ")

	verbPtr, _ := windows.UTF16PtrFromString(verb)
	exePtr, _ := windows.UTF16PtrFromString(exe)
	cwdPtr, _ := windows.UTF16PtrFromString(cwd)
	argsPtr, _ := windows.UTF16PtrFromString(args)

	var showCmd int32 = 1 // SW_SHOWNORMAL
	err = windows.ShellExecute(0, verbPtr, exePtr, argsPtr, cwdPtr, showCmd)
	if err != nil {
		fmt.Printf("Elevation request rejected: %v\n", err)
		time.Sleep(3 * time.Second)
	}
	os.Exit(0)
}

func listRemovableDrives() ([]usbDrive, error) {
	// Query external USB drives and media labeled as external or removable (covers NVMe enclosures)
	cmdText := `Get-Disk | Where-Object { ($_.BusType -eq 'USB' -or $_.MediaType -eq 'External' -or $_.MediaType -eq 'Removable') -and $_.OperationalStatus -eq 'Online' } | Select-Object Number, FriendlyName, Size | ConvertTo-Json -Compress`
	cmd := exec.Command("powershell", "-NoProfile", "-Command", cmdText)
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("powershell failed: %v", err)
	}

	trimmed := strings.TrimSpace(string(out))
	if trimmed == "" {
		return []usbDrive{}, nil
	}

	if !strings.HasPrefix(trimmed, "[") {
		trimmed = "[" + trimmed + "]"
	}

	type DiskInfo struct {
		Number       int    `json:"Number"`
		FriendlyName string `json:"FriendlyName"`
		Size         int64  `json:"Size"`
	}

	var rawDisks []DiskInfo
	err = json.Unmarshal([]byte(trimmed), &rawDisks)
	if err != nil {
		return nil, fmt.Errorf("json parse failed: %v", err)
	}

	var drives []usbDrive
	for _, d := range rawDisks {
		drives = append(drives, usbDrive{
			name:     strings.TrimSpace(d.FriendlyName),
			deviceId: fmt.Sprintf("%d", d.Number),
			sizeGB:   int(d.Size / (1024 * 1024 * 1024)),
		})
	}
	return drives, nil
}

func getUnusedLetters(count int) ([]string, error) {
	bitmask, err := windows.GetLogicalDrives()
	if err != nil {
		return nil, err
	}
	var letters []string
	for r := 'Z'; r >= 'D'; r-- {
		shift := uint32(r - 'A')
		if (bitmask & (1 << shift)) == 0 {
			letters = append(letters, string(r))
			if len(letters) == count {
				return letters, nil
			}
		}
	}
	return nil, fmt.Errorf("insufficient drive letters available")
}

func partitionDrive(diskNum string, l1, l2, l3 string) error {
	script := fmt.Sprintf(`select disk %s
clean
convert gpt
create partition primary size=200
format fs=fat32 quick label="EFI"
assign letter=%s
create partition primary size=512
format fs=fat32 quick label="BOOT"
assign letter=%s
create partition primary
format fs=exfat quick label="CHIMERA_DATA"
assign letter=%s
exit
`, diskNum, l1, l2, l3)

	tmpFile, err := os.CreateTemp("", "chimera-diskpart-*.txt")
	if err != nil {
		return err
	}
	defer os.Remove(tmpFile.Name())
	defer tmpFile.Close()

	_, err = tmpFile.WriteString(script)
	if err != nil {
		return err
	}

	cmd := exec.Command("diskpart", "/s", tmpFile.Name())
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func extractEmbedDir(srcDir string, destDir string) error {
	entries, err := fs.ReadDir(payload, srcDir)
	if err != nil {
		return err
	}

	err = os.MkdirAll(destDir, 0755)
	if err != nil {
		return err
	}

	for _, entry := range entries {
		srcPath := filepath.ToSlash(filepath.Join(srcDir, entry.Name()))
		destPath := filepath.Join(destDir, entry.Name())

		if entry.IsDir() {
			err = extractEmbedDir(srcPath, destPath)
			if err != nil {
				return err
			}
		} else {
			data, err := payload.ReadFile(srcPath)
			if err != nil {
				return err
			}
			err = os.WriteFile(destPath, data, 0644)
			if err != nil {
				return err
			}
		}
	}
	return nil
}

func extractEmbedFile(srcFile string, destFile string) error {
	data, err := payload.ReadFile(srcFile)
	if err != nil {
		return err
	}
	return os.WriteFile(destFile, data, 0644)
}

func removeMountLetters(letters ...string) {
	var builder strings.Builder
	for _, l := range letters {
		builder.WriteString(fmt.Sprintf("select volume %s\nremove letter=%s\n", l, l))
	}
	builder.WriteString("exit\n")

	tmpFile, err := os.CreateTemp("", "chimera-unmount-*.txt")
	if err != nil {
		return
	}
	defer os.Remove(tmpFile.Name())
	defer tmpFile.Close()

	tmpFile.WriteString(builder.String())
	
	cmd := exec.Command("diskpart", "/s", tmpFile.Name())
	cmd.Run()
}

func cleanupAndExit(code int) {
	fmt.Println("\nPress Enter to exit...")
	var dump string
	fmt.Scanln(&dump)
	os.Exit(code)
}
