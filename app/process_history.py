import os
import sqlite3
import csv
from datetime import datetime, timedelta
import pandas as pd

def extract_chrome_history(history_path, output_csv):
    """Extract browsing history from Chrome SQLite database"""
    try:
        # Connect to the SQLite database
        conn = sqlite3.connect(history_path)
        cursor = conn.cursor()

        # Query the history data
        cursor.execute(""" 
            SELECT urls.title, urls.url, urls.visit_count, urls.last_visit_time, 
                   datetime(urls.last_visit_time/1000000-11644473600,'unixepoch') as last_visit_date
            FROM urls
            ORDER BY last_visit_time DESC
        """)

        # Write to CSV
        with open(output_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Title', 'URL', 'VisitCount', 'LastVisitTime', 'LastVisitDate'])
            writer.writerows(cursor.fetchall())

        conn.close()
        return True
    except Exception as e:
        print(f"Error processing {history_path}: {str(e)}")
        return False


def process_all_students(input_folder, output_folder):
    """Process all student history files in a folder"""
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    student_data = []

    print(f"Looking for history files in: {input_folder}")
    print("Files in folder:", os.listdir(input_folder))

    # Iterate through all student history files
    for filename in os.listdir(input_folder):
        if filename.endswith('.csv') and '-history' in filename:
            trno = filename.split('-')[0]
            input_path = os.path.join(input_folder, filename)

            print(f"Identified student file: {filename} | TRNO: {trno}")

            student_data.append({'TRNO': trno, 'HistoryFile': input_path})

    return student_data


def combine_all_history(student_data, student_info_file, output_file):
    """Combine all student history with student info"""
    student_info = pd.read_excel(student_info_file)
    student_info['TRNO'] = student_info['TRNO'].astype(str)

    all_history = []

    for student in student_data:
        trno = student['TRNO']
        filtered = student_info[student_info['TRNO'] == trno]

        if filtered.empty:
            print(f"TRNO {trno} not found in Excel — skipping")
            continue

        student_record = filtered.iloc[0]

        try:
            history = pd.read_csv(student['HistoryFile'])

            history['TRNO'] = trno
            history['Class'] = f"{student_record['Class']}"
            
            history = history.rename(columns={
                'Title': 'title',
                'URL': 'url',
                'VisitCount': 'visit_count',
                'LastVisitDate': 'visit_time'
            })

            history['domain'] = history['url'].apply(lambda x: x.split('/')[2] if isinstance(x, str) and len(x.split('/')) > 2 else '')

            all_history.append(history[['TRNO', 'Class', 'title', 'url', 'domain', 'visit_time']])
        except Exception as e:
            print(f"Error processing {student['TRNO']}: {str(e)}")

    if all_history:
        combined = pd.concat(all_history, ignore_index=True)
        combined.to_csv(output_file, index=False)
        print(f"Processing complete. Combined data saved to {output_file}")
        return combined
    else:
        print("No history data to combine.")
        return None


if __name__ == "__main__":
    # Get the absolute path to the root project directory
    current_dir = os.path.dirname(os.path.abspath(__file__))  # This is the 'app' folder
    project_root = os.path.dirname(current_dir)  # This goes up to 'Student Browsing History Analysis System'

    # Build relative paths from the root directory
    input_folder = os.path.join(project_root, "processed_history")
    output_folder = os.path.join(project_root, "processed_history")
    student_info_file = os.path.join(project_root, "internal", "ALL FILES ARE HERE", "student list 45-46 without photos full.xls.xlsx")
    final_output = os.path.join(project_root, "browsing_history_data.csv")

    student_data = process_all_students(input_folder, output_folder)
    combined_data = combine_all_history(student_data, student_info_file, final_output)
