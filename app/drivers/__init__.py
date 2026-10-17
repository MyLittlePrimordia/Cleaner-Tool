"""Driver finder: detect, look up online, download, extract, install.

The engine behind the Drivers tab. Everything here is Tk-free and takes its
network and process access through small injectable functions, so the logic
(version comparison, query building, catalog parsing, install ordering) is
unit-tested without Windows and without the network.

Source of driver files: the Microsoft Update Catalog, searched by the
device's HARDWARE ID - the same lookup Windows' own "Update Driver" does and
the same one you can type into catalog.update.microsoft.com by hand. Every
package it returns is a Microsoft-signed, Windows-Update-published driver
cab for that hardware id, which is why it is safe to install: pnputil refuses
anything whose catalog signature does not verify. It is NOT a pack of
community drivers (SDIO) - coverage is whatever vendors have published to
Windows Update, which is less than SDIO for obscure or very old devices and
usually missing the very newest graphics drivers.

Modules:
    folder     where downloads go (next to the .exe, with a safe fallback)
    versions   driver version / date comparison
    inventory  what is installed, from Win32_PnPEntity + Win32_PnPSignedDriver
    catalog    search + download links from the Update Catalog
    download   streamed, allow-listed, hash-checked downloads with progress
    install    extract (expand.exe) and install (pnputil), with backup
    scan       inventory -> catalog -> "these can be updated"
"""
