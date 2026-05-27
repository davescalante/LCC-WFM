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
