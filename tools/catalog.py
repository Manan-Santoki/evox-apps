"""Turns `morphe list-patches -d -p -v -o -u` output into a JSON catalog.

usage: python3 catalog.py list-patches.txt > catalog.json
"""
import json
import re
import sys


def parse_option(lines):
    opt, field = {}, None
    for line in lines:
        line = line.rstrip()
        m = re.match(r"\t(Title|Description|Required|Key|Default|Type): ?(.*)$", line)
        if m:
            field = m.group(1).lower()
            opt[field] = m.group(2)
        elif re.match(r"\tPossible values:", line):
            field = "values"
            opt["values"] = []
        elif field == "values" and line.startswith("\t\t"):
            # "value (Label)", the label may itself contain parentheses
            value, _, label = line.strip().partition(" (")
            opt["values"].append({"value": value, "label": label[:-1] if label else value})
        elif field == "description":
            opt["description"] += "\n" + line.strip()
    opt["description"] = opt.get("description", "").strip()
    opt["required"] = opt.get("required") == "true"
    opt["type"] = opt.get("type", "").replace("kotlin.", "")
    if opt["type"] == "Boolean" and "default" in opt:
        opt["default"] = opt["default"] == "true"
    return opt


def parse_block(block):
    lines = block.split("\n")
    patch = {"name": lines[0][len("Name: "):].strip(), "description": "", "options": [],
             "packages": {}}
    section, buf, pkg = None, [], None
    for line in lines[1:]:
        if line.startswith("Description: "):
            section = "description"
            patch["description"] = line[len("Description: "):]
        elif line.startswith("Enabled: "):
            patch["enabled"] = line.endswith("true")
            section = None
        elif line == "Options:":
            section = "options"
        elif line == "Compatible packages:":
            if buf:
                patch["options"].append(parse_option(buf))
                buf = []
            section = "packages"
        elif section == "options":
            if line.startswith("\tTitle:") and buf:
                patch["options"].append(parse_option(buf))
                buf = []
            buf.append(line)
        elif section == "packages":
            m = re.match(r"\tPackage name: (\S+)", line)
            if m:
                pkg = m.group(1)
                patch["packages"][pkg] = []
            elif line.startswith("\t\t") and pkg:
                patch["packages"][pkg].append(line.strip())
        elif section == "description" and line.strip():
            patch["description"] += "\n" + line.strip()
    if buf:
        patch["options"].append(parse_option(buf))
    patch["description"] = patch["description"].strip()
    return patch


def main():
    text = open(sys.argv[1]).read().replace("INFO: Name:", "Name:")
    blocks = [b for b in re.split(r"\n(?=Name: )", text) if b.startswith("Name: ")]
    print(json.dumps([parse_block(b) for b in blocks], indent=1))


if __name__ == "__main__":
    main()
