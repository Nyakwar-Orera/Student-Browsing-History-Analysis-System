import os
import csv
import pandas as pd
import chardet

# === Step 0: Set up relative paths based on script location ===
current_dir = os.path.dirname(os.path.abspath(__file__))  # e.g., .../Student Browsing History Analysis System
project_root = current_dir  # Since the script is in the root, we can use current_dir

input_path = os.path.join(project_root, "browsing_history_data.csv")
output_path = os.path.join(project_root, "browsing_history_data_cleaned.csv")

# === Step 1: Detect encoding ===
def detect_encoding(file_path, sample_size=10000):
    with open(file_path, 'rb') as f:
        raw = f.read(sample_size)
    result = chardet.detect(raw)
    return result['encoding']

# === Step 2: Log issues and clean ===
def clean_csv(input_path, output_path):
    encoding = detect_encoding(input_path)
    print(f"Detected encoding: {encoding}")

    issues = []

    # First pass to log malformed rows and column count inconsistencies
    with open(input_path, 'r', encoding=encoding, newline='') as f:
        sample = csv.reader(f)
        headers = next(sample)
        expected_columns = len(headers)
        print(f"Expected columns: {expected_columns}")

        for i, row in enumerate(sample, start=2):  # start=2 due to header
            if len(row) != expected_columns:
                issues.append(f"Line {i}: Expected {expected_columns} columns, found {len(row)}")

    # Report issues
    if issues:
        print(f"Found {len(issues)} issue(s):")
        for issue in issues[:10]:
            print(" -", issue)
        if len(issues) > 10:
            print(f" - ...and {len(issues) - 10} more.")

    # === Step 3: Read with pandas and clean ===
    df = pd.read_csv(input_path, encoding=encoding, on_bad_lines='skip')

    # Strip whitespace from string fields
    df = df.applymap(lambda x: x.strip() if isinstance(x, str) else x)

    # Log missing values
    missing = df.isnull().sum()
    missing_total = missing.sum()
    if missing_total > 0:
        print(f"Missing values found in {missing[missing > 0].count()} columns, total: {missing_total}")

    # Fill missing values with the mode (most frequent value) of each column
    if 'title' in df.columns:
        df['title'].fillna(df['title'].mode()[0], inplace=True)
    if 'domain' in df.columns:
        df['domain'].fillna(df['domain'].mode()[0], inplace=True)

    # Save cleaned file
    df.to_csv(output_path, index=False)
    print(f"Cleaned file saved to:\n{output_path}")

# === Run it ===
clean_csv(input_path, output_path)
