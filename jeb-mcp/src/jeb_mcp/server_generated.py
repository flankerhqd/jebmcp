# NOTE: This file has been automatically generated, do not modify!
# Architecture based on https://github.com/mrexodia/ida-pro-mcp (MIT License)
from typing import Annotated, TypeVar

T = TypeVar("T")



@mcp.tool()
def get_manifest(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
) -> str:
    """
    Get the AndroidManifest.xml of the given APK as plain text.
    Use this to identify package name, activities, services, and permissions.
    """
    return make_jsonrpc_request("get_manifest", filepath)


@mcp.tool()
def get_exported_components(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    component_type: Annotated[
        str, "Type of component: 'activity', 'service', 'receiver', or 'provider'."
    ],
) -> list[str]:
    """
    Get all exported components of the specified type from the APK manifest.
    A component is considered "exported" if it sets android:exported="true"
    or has an <intent-filter> without explicitly setting exported="false".
    """
    return make_jsonrpc_request("get_exported_components", filepath, component_type)


@mcp.tool()
def get_decompiled_code(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    item_signature: Annotated[
        str,
        "The fully-qualified signature to decompile, e.g., 'Lcom/abc/Foo;' or 'Lcom/abc/Foo;->bar(I)V'",
    ],
) -> str:
    """
    Get the decompiled Java code (pseudo-code) of the given class or method.
    If providing a class signature, the entire class is decompiled.
    """
    return make_jsonrpc_request("get_decompiled_code", filepath, item_signature)


@mcp.tool()
def get_smali_code(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    item_signature: Annotated[
        str,
        "The fully-qualified signature, e.g., 'Lcom/abc/Foo;' or 'Lcom/abc/Foo;->bar(I)V'",
    ],
) -> str:
    """
    Get the smali (assembly-like) code of the given class or method.
    Useful for verifying low-level logic or specific instruction offsets.
    """
    return make_jsonrpc_request("get_smali_code", filepath, item_signature)


@mcp.tool()
def get_method_overrides(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    method_signature: Annotated[
        str,
        "Full method signature, e.g., 'Lcom/abc/Foo;->onCreate(Landroid/os/Bundle;)V'",
    ],
) -> list[str]:
    """
    Get the overrides of the given method (up the inheritance chain).
    Helps understand the relationships between methods in parent classes/interfaces.
    """
    return make_jsonrpc_request("get_method_overrides", filepath, method_signature)


@mcp.tool()
def get_class_hierarchy(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    class_signature: Annotated[
        str, "Fully-qualified signature of the class, e.g., 'Lcom/abc/Foo;'"
    ],
    relation_type: Annotated[str, "Type of relation: 'superclass' or 'interface'."],
) -> list[str] | str:
    """Get the superclass or interfaces of the given class."""
    return make_jsonrpc_request(
        "get_class_hierarchy", filepath, class_signature, relation_type
    )


@mcp.tool()
def get_class_members(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    class_signature: Annotated[
        str, "Fully-qualified signature of the class, e.g., 'Lcom/abc/Foo;'"
    ],
    member_type: Annotated[str, "Type of member: 'method' or 'field'."],
) -> list[str]:
    """Get a list of all method or field signatures defined in the given class."""
    return make_jsonrpc_request(
        "get_class_members", filepath, class_signature, member_type
    )


@mcp.tool()
def rename_code_item(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    item_signature: Annotated[
        str, "Full signature of the class, method, or field to rename."
    ],
    new_name: Annotated[str, "The new descriptive name for the item."],
):
    """
    Rename a class, method, or field globally in the project.
    Changes both the internal DEX model and the decompiled view.
    """
    return make_jsonrpc_request("rename_code_item", filepath, item_signature, new_name)


@mcp.tool()
def check_java_identifier(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    identifier: Annotated[
        str, "Dot-notation name (com.abc.Foo) or signature (Lcom/abc/Foo;)"
    ],
) -> list[dict]:
    """
    Check an identifier and recognize if it is a class, method, or field.
    Returns details including signature and type. Use this BEFORE other tools to verify inputs.
    """
    return make_jsonrpc_request("check_java_identifier", filepath, identifier)


@mcp.tool()
def list_cross_references(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    address: Annotated[
        str, "The address or signature (Lcom/abc/Foo;->bar()V) to query XREFs for."
    ],
) -> list[dict]:
    """Retrieve callers (XREFs) of the item at the provided address or signature."""
    return make_jsonrpc_request("list_cross_references", filepath, address)


@mcp.tool()
def rename_pseudo_code_variables(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    method_signature: Annotated[
        str, "Fully-qualified signature of the containing method."
    ],
    old_var_name: Annotated[
        str, "The current name (e.g., 'v0', 'p1') of the variable."
    ],
    new_var_name: Annotated[str, "The new descriptive name for the variable."],
):
    """
    Rename local variables or parameters within a decompiled method's pseudo-code.
    Note: The method must be decompiled (accessible via get_decompiled_code) first.
    """
    return make_jsonrpc_request(
        "rename_pseudo_code_variables",
        filepath,
        method_signature,
        old_var_name,
        new_var_name,
    )


@mcp.tool()
def list_dex_strings(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
) -> list[str]:
    """Retrieve all strings from the DEX constant pools. Useful for searching hardcoded keys or URLs."""
    return make_jsonrpc_request("list_dex_strings", filepath)


@mcp.tool()
def get_all_classes(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
) -> list[str]:
    """List all class signatures in the APK (from the main DEX unit)."""
    return make_jsonrpc_request("get_all_classes", filepath)


@mcp.tool()
def get_apk_all_files(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    category: Annotated[str, "Type of files: 'resource' or 'asset'."],
) -> list[str]:
    """Retrieve all file names for a given category (resource or asset) present in the APK."""
    return make_jsonrpc_request("get_apk_all_files", filepath, category)


@mcp.tool()
def get_apk_file_content(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    file_path: Annotated[
        str,
        "The path to the file, e.g., 'res/values/strings.xml' or 'assets/config.json'",
    ],
    category: Annotated[str, "Type of file: 'resource' or 'asset'."],
) -> str:
    """
    Retrieve the text content of a resource or asset file given its path.
    Supports obfuscated paths via a powerful fallback locator.
    """
    return make_jsonrpc_request("get_apk_file_content", filepath, file_path, category)


@mcp.tool()
def add_comment(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    address: Annotated[
        str, "The address or signature where the comment should be added."
    ],
    comment: Annotated[str, "The comment text to add."],
):
    """
    Add a comment to a class, method, field, or specific instruction address.
    Visible in both Smali and Java views. Supports Unicode characters.
    """
    return make_jsonrpc_request("add_comment", filepath, address, comment)


@mcp.tool()
def search_in_project(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    query: Annotated[str, "The search term or regular expression."],
    search_type: Annotated[str, "Type of search: 'string' or 'identifier'."] = "string",
) -> list[dict]:
    """
    Search for strings or identifiers (classes/methods) in the project.
    Useful for finding hardcoded domains, secrets, or specific obfuscated names.
    search_type can be 'string' (default), 'identifier', 'resource', 'asset', or 'native'.
    - 'string': search DEX string pool for matching values.
    - 'identifier': search class/method signatures.
    - 'resource': search resource file paths and text content (xml, json, etc).
    - 'asset': search asset file paths and text content.
    """
    return make_jsonrpc_request("search_in_project", filepath, query, search_type)


@mcp.tool()
def perform_security_scan(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
) -> list[dict]:
    """
    Performs a comprehensive security scan on the APK, covering three areas:
    1. Packer Detection: Identifies known packers/protectors (e.g., Qihoo360, Bangcle, Ijiami) via file signature matching (rules from 'apkpackdata.json').
    2. SDK Identification: Detects embedded third-party SDKs by matching native library names (rules from 'sdk.json').
    3. Sensitive String/API Scan: Searches the DEX string pool, method and class signatures for sensitive patterns like hardcoded keys, URLs, crypto APIs, etc. (rules from 'sensitive_strings.txt').
    Returns a flat list of dicts, each with 'type' ('Packer', 'SDK', or 'Sensitive String'), 'name', and 'detail'.
    """
    return make_jsonrpc_request("perform_security_scan", filepath)


@mcp.tool()
def export_all_resources(
    filepath: Annotated[str, "The absolute filesystem path to the APK file. If the APK is already open in JEB, you can pass an empty string \"\"."],
    output_dir: Annotated[
        str, "The absolute path to the local directory where resources will be saved."
    ],
) -> dict:
    """
    Export all accessible resources and assets to a local directory structure.
    Enables deep analysis with external grep or auditing tools.
    """
    return make_jsonrpc_request("export_all_resources", filepath, output_dir)
