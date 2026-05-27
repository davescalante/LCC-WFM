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


# File size
def test_directory_size_within_limit():
    total = sum(
        os.path.getsize(os.path.join(dirpath, f))
        for dirpath, _, files in os.walk(PATH)
        for f in files
    )
    assert total < 1 * 1024 ** 3, f"Directory exceeds 1GB: {total} bytes"
