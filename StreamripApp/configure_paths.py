import os
import pathlib

def configure():
    # The extension is located one level up from the StreamripApp directory
    project_root = pathlib.Path(__file__).parent.parent.absolute()
    extension_path = project_root / "flet_audio_service"
    
    # Convert to a valid file:// URL
    # On Windows, this needs to look like file:///C:/path/to/ext
    if os.name == 'nt':
        ext_url = f"file:///{extension_path.as_posix()}"
    else:
        ext_url = f"file://{extension_path.absolute()}"
        
    print(f"Auto-resolving flet_audio_service to: {ext_url}")
    
    config_file = pathlib.Path(__file__).parent / "pyproject.toml"
    
    if not config_file.exists():
        print("Error: pyproject.toml not found!")
        return

    content = config_file.read_text()
    
    # We replace the placeholder with the actual absolute path
    # OR we replace any existing file:// URL if the user already ran the script
    import re
    placeholder_pattern = r"\"flet_audio_service @ [^\"]+\""
    new_line = f'\"flet_audio_service @ {ext_url}\"'
    
    new_content = re.sub(placeholder_pattern, new_line, content)
    
    if new_content != content:
        config_file.write_text(new_content)
        print("Successfully updated pyproject.toml with local absolute path.")
    else:
        print("No changes needed in pyproject.toml.")

if __name__ == "__main__":
    configure()
