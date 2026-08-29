import xml.etree.ElementTree as ET

root = ET.Element("assembly", {
    "xmlns": "urn:schemas-microsoft-com:asm.v1",
    "manifestVersion": "1.0"
})

identity = ET.SubElement(root, "assemblyIdentity", {
    "version": "1.0.0.0",
    "name": "CleanerTool",
    "processorArchitecture": "*"
})

desc = ET.SubElement(root, "description")
desc.text = "Cleaner Tool"

trust = ET.SubElement(root, "trustInfo", {
    "xmlns": "urn:schemas-microsoft-com:asm.v2"
})
security = ET.SubElement(trust, "security")
priv = ET.SubElement(security, "requestedPrivileges")
ET.SubElement(priv, "requestedExecutionLevel", {
    "level": "requireAdministrator",
    "uiAccess": "false"
})

compat = ET.SubElement(root, "compatibility", {
    "xmlns": "urn:schemas-microsoft-com:compatibility.v1"
})
app = ET.SubElement(compat, "application")
ET.SubElement(app, "supportedOS", {"Id": "{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"})
ET.SubElement(app, "supportedOS", {"Id": "{1f676c76-80e1-4239-95bb-83d0f6d0da78}"})

app2 = ET.SubElement(root, "application", {
    "xmlns": "urn:schemas-microsoft-com:asm.v3"
})
ws = ET.SubElement(app2, "windowsSettings")
dpi1 = ET.SubElement(ws, "dpiAware", {
    "xmlns": "http://schemas.microsoft.com/SMI/2005/WindowsSettings"
})
dpi1.text = "True/PM"
dpi2 = ET.SubElement(ws, "dpiAwareness", {
    "xmlns": "http://schemas.microsoft.com/SMI/2016/WindowsSettings"
})
dpi2.text = "PerMonitorV2"

tree = ET.ElementTree(root)
# Add XML declaration manually since standalone isn't supported
import io
buf = io.BytesIO()
tree.write(buf, encoding="UTF-8", xml_declaration=True)
content = buf.getvalue().replace(b'?>', b' standalone="yes"?>')
with open("app/assets/admin_manifest.xml", "wb") as f:
    f.write(content)
print("Written valid manifest")