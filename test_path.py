import os
import tempfile

PATH = "/Users/denisetijerina/Documents/LCC-WFM"


# Existence & type
def test_path_exists():
    assert os.path.exists(PATH), f"Path does not exist: {PATH}"

def test_path_is_directory():
    assert os.path.isdir(PATH), f"Path is not a directory: {PATH}"

def test_path_is_not_symlink():
    assert not os.path.islink(PATH), f"Path is a symlink: {PATH}"

def test_path_is_absolute():
    assert os.path.isabs(PATH), f"Path is not absolute: {PATH}"

def test_path_length():
    assert len(PATH) <= 260, f"Path exceeds maximum length: {PATH}"


# Permissions
def test_path_is_readable():
    assert os.access(PATH, os.R_OK), f"Path is not readable: {PATH}"

def test_path_is_writable():
    assert os.access(PATH, os.W_OK), f"Path is not writable: {PATH}"

def test_path_is_executable():
    assert os.access(PATH, os.X_OK), f"Path is not executable: {PATH}"


# File operations
def test_can_create_file():
    tmp = os.path.join(PATH, ".test_write_tmp")
    with open(tmp, "w") as f:
        f.write("test")
    assert os.path.exists(tmp)
    os.remove(tmp)

def test_can_delete_file():
    tmp = os.path.join(PATH, ".test_delete_tmp")
    with open(tmp, "w") as f:
        f.write("test")
    os.remove(tmp)
    assert not os.path.exists(tmp)


# Directory contents
def test_directory_is_not_empty():
    assert len(os.listdir(PATH)) > 0, f"Directory is empty: {PATH}"

def test_expected_file_exists():
    assert os.path.exists(os.path.join(PATH, "test_path.py")), "test_path.py not found"


# Ownership
def test_path_owned_by_current_user():
    stat = os.stat(PATH)
    assert stat.st_uid == os.getuid(), "Path is not owned by the current user"


# File permissions
def test_file_is_readable():
    filepath = os.path.join(PATH, "test_path.py")
    assert os.access(filepath, os.R_OK), f"File is not readable: {filepath}"

def test_file_is_writable():
    filepath = os.path.join(PATH, "test_path.py")
    assert os.access(filepath, os.W_OK), f"File is not writable: {filepath}"

def test_file_permissions_not_world_writable():
    filepath = os.path.join(PATH, "test_path.py")
    mode = os.stat(filepath).st_mode
    assert not (mode & 0o002), f"File is world-writable: {filepath}"


# File creation date
def test_file_creation_date_is_in_the_past():
    filepath = os.path.join(PATH, "test_path.py")
    import time
    ctime = os.path.getctime(filepath)
    assert ctime < time.time(), f"File creation date is in the future: {filepath}"

def test_file_creation_date_is_recent():
    filepath = os.path.join(PATH, "test_path.py")
    import time
    ctime = os.path.getctime(filepath)
    age_days = (time.time() - ctime) / 86400
    assert age_days < 365, f"File is older than 1 year: {filepath}"


# File modification date
def test_file_modification_date_is_in_the_past():
    filepath = os.path.join(PATH, "test_path.py")
    import time
    mtime = os.path.getmtime(filepath)
    assert mtime < time.time(), f"File modification date is in the future: {filepath}"

def test_file_modification_date_is_recent():
    filepath = os.path.join(PATH, "test_path.py")
    import time
    mtime = os.path.getmtime(filepath)
    age_days = (time.time() - mtime) / 86400
    assert age_days < 365, f"File was not modified in the last year: {filepath}"

def test_file_modified_after_created():
    filepath = os.path.join(PATH, "test_path.py")
    birthtime = os.stat(filepath).st_birthtime  # macOS actual creation time
    mtime = os.path.getmtime(filepath)
    assert mtime >= birthtime, f"File modification date is before creation date: {filepath}"


# File content
def test_file_is_not_empty():
    filepath = os.path.join(PATH, "test_path.py")
    assert os.path.getsize(filepath) > 0, f"File is empty: {filepath}"

def test_file_contains_expected_content():
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "r") as f:
        content = f.read()
    assert "def test_" in content, "File does not contain any test functions"

def test_file_is_valid_utf8():
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "r", encoding="utf-8") as f:
        f.read()

def test_file_is_valid_python():
    import ast
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "r") as f:
        source = f.read()
    ast.parse(source)


# File path normalization
def test_path_is_normalized():
    assert os.path.normpath(PATH) == PATH, f"Path is not normalized: {PATH}"

def test_normalized_path_removes_double_slashes():
    messy = "/Users/denisetijerina//Documents/LCC-WFM"
    assert os.path.normpath(messy) == PATH, f"normpath did not clean double slashes: {messy}"

def test_normalized_path_removes_dotdot():
    messy = "/Users/denisetijerina/Documents/LCC-WFM/../LCC-WFM"
    assert os.path.normpath(messy) == PATH, f"normpath did not resolve '..': {messy}"

def test_normalized_path_removes_dot():
    messy = "/Users/denisetijerina/Documents/./LCC-WFM"
    assert os.path.normpath(messy) == PATH, f"normpath did not resolve '.': {messy}"


# File path resolution
def test_path_resolves_to_itself():
    from pathlib import Path
    assert Path(PATH).resolve() == Path(PATH), "Path does not resolve to itself"

def test_resolved_path_is_absolute():
    from pathlib import Path
    assert Path(PATH).resolve().is_absolute(), "Resolved path is not absolute"

def test_resolved_path_has_no_dotdot():
    from pathlib import Path
    assert ".." not in str(Path(PATH).resolve()), "Resolved path contains '..'"

def test_resolved_path_has_no_dot():
    from pathlib import Path
    parts = Path(PATH).resolve().parts
    assert "." not in parts, "Resolved path contains '.'"


# File path comparison
def test_path_equals_itself():
    from pathlib import Path
    assert Path(PATH) == Path(PATH), "Path does not equal itself"

def test_path_equals_string_constructed_path():
    from pathlib import Path
    assert Path(PATH) == Path("/Users/denisetijerina/Documents/LCC-WFM"), "Path does not equal equivalent path"

def test_path_not_equal_to_parent():
    from pathlib import Path
    assert Path(PATH) != Path(PATH).parent, "Path should not equal its parent"

def test_path_not_equal_to_different_path():
    from pathlib import Path
    assert Path(PATH) != Path("/tmp"), "Path should not equal /tmp"


# File path string representation
def test_path_str_is_string():
    from pathlib import Path
    assert isinstance(str(Path(PATH)), str), "Path string representation is not a string"

def test_path_str_is_correct():
    from pathlib import Path
    assert str(Path(PATH)) == PATH, f"Path string mismatch: {str(Path(PATH))}"

def test_path_repr_contains_path():
    from pathlib import Path
    assert PATH in repr(Path(PATH)), f"Path not found in repr: {repr(Path(PATH))}"

def test_path_str_has_no_trailing_slash():
    from pathlib import Path
    assert not str(Path(PATH)).endswith("/"), f"Path has trailing slash: {PATH}"


# File path parts
def test_path_has_parts():
    from pathlib import Path
    parts = Path(PATH).parts
    assert len(parts) > 0, f"Path has no parts: {PATH}"

def test_path_parts_are_correct():
    from pathlib import Path
    parts = Path(PATH).parts
    assert parts == ("/", "Users", "denisetijerina", "Documents", "LCC-WFM"), f"Unexpected parts: {parts}"

def test_path_parts_first_is_root():
    from pathlib import Path
    parts = Path(PATH).parts
    assert parts[0] == "/", f"First part is not root: {parts[0]}"

def test_path_parts_last_is_basename():
    from pathlib import Path
    parts = Path(PATH).parts
    assert parts[-1] == "LCC-WFM", f"Last part is not basename: {parts[-1]}"


# File path anchor
def test_path_has_anchor():
    from pathlib import Path
    anchor = Path(PATH).anchor
    assert anchor, f"Path has no anchor: {PATH}"

def test_path_anchor_is_correct():
    from pathlib import Path
    anchor = Path(PATH).anchor
    assert anchor == "/", f"Expected '/' anchor on macOS, got: {anchor}"

def test_path_starts_with_anchor():
    from pathlib import Path
    p = Path(PATH)
    assert PATH.startswith(p.anchor), f"Path does not start with its anchor: {PATH}"


# File path drive
def test_path_drive_is_empty_on_macos():
    from pathlib import Path
    drive = Path(PATH).drive
    assert drive == "", f"Expected no drive on macOS, got: {drive}"

def test_path_splitdrive_returns_expected():
    drive, tail = os.path.splitdrive(PATH)
    assert drive == "", f"Expected empty drive on macOS, got: {drive}"
    assert tail == PATH, f"Expected tail to be full path, got: {tail}"


# File path suffix
def test_file_has_suffix():
    from pathlib import Path
    suffix = Path(os.path.join(PATH, "test_path.py")).suffix
    assert suffix, "File has no suffix"

def test_file_suffix_is_correct():
    from pathlib import Path
    suffix = Path(os.path.join(PATH, "test_path.py")).suffix
    assert suffix == ".py", f"Expected '.py', got: {suffix}"

def test_file_suffix_starts_with_dot():
    from pathlib import Path
    suffix = Path(os.path.join(PATH, "test_path.py")).suffix
    assert suffix.startswith("."), f"Suffix does not start with a dot: {suffix}"


# File path stem
def test_file_has_stem():
    from pathlib import Path
    stem = Path(os.path.join(PATH, "test_path.py")).stem
    assert stem, "File has no stem"

def test_file_stem_is_correct():
    from pathlib import Path
    stem = Path(os.path.join(PATH, "test_path.py")).stem
    assert stem == "test_path", f"Expected 'test_path', got: {stem}"

def test_file_stem_has_no_spaces():
    from pathlib import Path
    stem = Path(os.path.join(PATH, "test_path.py")).stem
    assert " " not in stem, f"Stem contains spaces: {stem}"


# File path basename
def test_path_has_basename():
    basename = os.path.basename(PATH)
    assert basename, f"Path has no basename: {PATH}"

def test_path_basename_is_correct():
    basename = os.path.basename(PATH)
    assert basename == "LCC-WFM", f"Expected 'LCC-WFM', got: {basename}"

def test_path_basename_has_no_spaces():
    basename = os.path.basename(PATH)
    assert " " not in basename, f"Basename contains spaces: {basename}"


# File path root directory
def test_path_has_root():
    root = os.path.splitdrive(PATH)[1][0] if os.path.splitdrive(PATH)[0] else PATH[0]
    assert root == "/", f"Path does not start from root: {PATH}"

def test_root_directory_exists():
    assert os.path.exists("/"), "Root directory does not exist"

def test_root_directory_is_directory():
    assert os.path.isdir("/"), "Root is not a directory"


# File path parent directory
def test_parent_directory_exists():
    parent = os.path.dirname(PATH)
    assert os.path.exists(parent), f"Parent directory does not exist: {parent}"

def test_parent_directory_is_correct():
    parent = os.path.dirname(PATH)
    assert parent == "/Users/denisetijerina/Documents", f"Unexpected parent directory: {parent}"

def test_parent_directory_is_directory():
    parent = os.path.dirname(PATH)
    assert os.path.isdir(parent), f"Parent is not a directory: {parent}"


# File path depth
def test_path_depth_is_positive():
    parts = [p for p in PATH.split(os.sep) if p]
    assert len(parts) > 0, f"Path has no depth: {PATH}"

def test_path_depth_within_limit():
    parts = [p for p in PATH.split(os.sep) if p]
    assert len(parts) <= 20, f"Path is too deep ({len(parts)} levels): {PATH}"

def test_path_depth_is_expected():
    parts = [p for p in PATH.split(os.sep) if p]
    assert len(parts) == 4, f"Expected depth of 4, got {len(parts)}: {PATH}"


# File path separator
def test_path_uses_correct_separator():
    assert os.sep == "/", f"Expected '/' separator on this OS, got: {os.sep}"

def test_path_contains_no_backslashes():
    assert "\\" not in PATH, f"Path contains backslashes: {PATH}"

def test_path_components_are_non_empty():
    parts = PATH.split(os.sep)
    non_empty = [p for p in parts if p]
    assert len(non_empty) > 0, f"Path has no valid components: {PATH}"


# File character count
def test_file_has_characters():
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "r") as f:
        content = f.read()
    assert len(content) > 0, f"File has no characters: {filepath}"

def test_file_character_count_within_limit():
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "r") as f:
        content = f.read()
    assert len(content) <= 100000, f"File exceeds 100,000 characters: {len(content)}"


# File word count
def test_file_has_words():
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "r") as f:
        words = f.read().split()
    assert len(words) > 0, f"File has no words: {filepath}"

def test_file_word_count_within_limit():
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "r") as f:
        words = f.read().split()
    assert len(words) <= 10000, f"File exceeds 10000 words: {len(words)}"


# File line count
def test_file_has_lines():
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "r") as f:
        lines = f.readlines()
    assert len(lines) > 0, f"File has no lines: {filepath}"

def test_file_line_count_within_limit():
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "r") as f:
        lines = f.readlines()
    assert len(lines) <= 1000, f"File exceeds 1000 lines: {len(lines)}"

def test_file_has_no_blank_only_lines_at_end():
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "r") as f:
        content = f.read()
    assert not content.endswith("\n\n"), f"File ends with multiple blank lines: {filepath}"


# File encoding
def test_file_is_utf8_encoded():
    import codecs
    filepath = os.path.join(PATH, "test_path.py")
    try:
        with codecs.open(filepath, encoding="utf-8", errors="strict") as f:
            f.read()
    except UnicodeDecodeError:
        assert False, f"File is not UTF-8 encoded: {filepath}"

def test_file_has_no_null_bytes():
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "rb") as f:
        content = f.read()
    assert b"\x00" not in content, f"File contains null bytes: {filepath}"

def test_file_has_consistent_line_endings():
    filepath = os.path.join(PATH, "test_path.py")
    with open(filepath, "rb") as f:
        content = f.read()
    assert b"\r\n" not in content, f"File contains Windows-style line endings: {filepath}"


# File name
def test_file_has_name():
    filepath = os.path.join(PATH, "test_path.py")
    name = os.path.basename(filepath)
    assert name, f"File has no name: {filepath}"

def test_file_has_correct_name():
    filepath = os.path.join(PATH, "test_path.py")
    name = os.path.basename(filepath)
    assert name == "test_path.py", f"Expected test_path.py, got: {name}"

def test_file_name_has_no_spaces():
    filepath = os.path.join(PATH, "test_path.py")
    name = os.path.basename(filepath)
    assert " " not in name, f"File name contains spaces: {name}"


# File extension
def test_file_has_extension():
    filepath = os.path.join(PATH, "test_path.py")
    _, ext = os.path.splitext(filepath)
    assert ext, f"File has no extension: {filepath}"

def test_file_has_correct_extension():
    filepath = os.path.join(PATH, "test_path.py")
    _, ext = os.path.splitext(filepath)
    assert ext == ".py", f"Expected .py extension, got: {ext}"


# File size
def test_directory_size_within_limit():
    total = sum(
        os.path.getsize(os.path.join(dirpath, f))
        for dirpath, _, files in os.walk(PATH)
        for f in files
    )
    assert total < 1 * 1024 ** 3, f"Directory exceeds 1GB: {total} bytes"
