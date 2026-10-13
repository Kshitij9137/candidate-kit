import os

project_dir = r"D:\PROJECTS\SDT_candidate_kit\candidate_kit"  # change this
output_file = "project_code.txt"

# Only include meaningful code files
include_extensions = {
    ".py", ".js", ".ts", ".html", ".css",
    ".cpp", ".c", ".java", ".json", ".docx",
    ".md", ".yaml", ".env"
}

# Folders to ignore
exclude_dirs = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "site-packages",
    "dist",
    "build",
    ".next",
    ".vscode",
}

# Files to ignore
exclude_files = {
    "package-lock.json", "yarn.lock", "export_code.py", "project_code.py"
}

# Max file size (in bytes) -> 200 KB
MAX_FILE_SIZE = 200 * 1024

total_lines = 0

with open(output_file, "w", encoding="utf-8") as outfile:
    for root, dirs, files in os.walk(project_dir):

        # Remove unwanted directories
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        for file in files:
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, project_dir)

            # Skip unwanted files
            if file in exclude_files:
                continue

            # Skip by extension
            _, ext = os.path.splitext(file)
            if ext.lower() not in include_extensions:
                continue

            # Skip minified files
            if ".min." in file:
                continue

            # Skip large files
            if os.path.getsize(file_path) > MAX_FILE_SIZE:
                continue

            try:
                with open(file_path, "r", encoding="utf-8") as infile:
                    lines = infile.readlines()

                # Skip files with too many lines (likely generated)
                if len(lines) > 2000:
                    continue

                outfile.write("\n" + "="*70 + "\n")
                outfile.write(f"FILE: {rel_path}\n")
                outfile.write("="*70 + "\n")

                outfile.writelines(lines)
                total_lines += len(lines)

            except:
                continue

print(f"✅ Done! Clean code exported. Total lines: {total_lines}")