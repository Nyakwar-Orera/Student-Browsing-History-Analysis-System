
# Student Browsing History Analysis System
# Browsing History Data Cleaning, Processing & Analysis

This project involves cleaning, processing, and analyzing browsing history data using Python and Flask. It provides a secure admin dashboard for viewing and interacting with detailed reports and analytics, with options to export data in CSV and PDF formats.

## 🚀 Deployment Instructions

### 1. 📦 Install Required Packages
Start by installing the necessary dependencies. Open your terminal and run the following command:
```bash
pip install -r requirements.txt

```

### 2. 🛠 Create a `.env` File
In your project root directory, create a file named `.env` and add the following configuration:
```
FLASK_APP=main.py
FLASK_ENV=development
SECRET_KEY=your-secret-key-here
```

> 🔐 Replace `your-secret-key-here` with a strong secret key for session security.

### 3. 🧹 Run the Data Processing Script
Before launching the application, process the browsing history data using the `process_history.py` script. Run the following command:
```bash
python process_history.py
```

### 4. ▶️ Start the Flask Application
Launch the Flask server to run the application:
```bash
flask run
```

### 5. 🌐 Access the Application
Once the server is running, open your browser and visit:
[http://localhost:5000](http://localhost:5000)

---

## 🔑 Key Features Implemented

### ✅ Secure Admin Login
- **Password**: `admin100%SECURED`  
- Provides authenticated access to the dashboards and analytics.

### 📊 Dashboard (`home.html`)
- Displays a full browsing history table.
- Filter by **TRNO** and **Date Range**.
- Includes pagination for large data sets.
- Visual charts and summaries for easier data interpretation.
- Export options: **CSV**, **PDF**.

### 📄 Student Reports (`report.html`)
- Per-student statistics, including:
  - Most visited domains.
  - Usage time tracking.
  - Detailed activity logs.

### 📈 Analytics Page (`analytics.html`)
- View aggregate statistics of overall browsing activity.
- Identify usage behavior and patterns.
- Detect potential issues and anomalies.
- Interactive visualizations to explore the data.

---

## 🧰 Data Cleaning & Processing

Before running the application, you may need to clean and process the browsing history data. This involves:

1. **Cleaning the Raw Data**
   - The `cleaningScript.py` handles issues like encoding, missing values, and incorrect columns in the raw CSV file.
   
2. **Configure File Paths**
   - Ensure that you update the file paths in the Python script to point to your raw and cleaned CSV files:
   ```python
   input_path = r"C:\path\to\browsing_history_data.csv"
   output_path = r"C:\path\to\browsing_history_data_cleaned.csv"
   ```

3. **Run the Cleaning Script**
   - Execute the `cleaningScript.py` file to clean the raw dataset:
   ```bash
   python cleaningScript.py
   ```

4. **Load Cleaned Data**
   After cleaning, you can load the cleaned dataset for analysis:
   ```python
   import pandas as pd
   cleaned_data_path = r"C:\path\to\browsing_history_data_cleaned.csv"
   df_cleaned = pd.read_csv(cleaned_data_path)
   print(df_cleaned.head())
   ```


## 💡 Tech Highlights
- **Tailwind CSS**: For responsive and clean design.
- **html2pdf**: For exporting data and reports in PDF format.
- **pandas**: For data handling and processing.
- **dotenv**: To securely manage configuration, including the Flask secret key.
- **chardet**: For detecting file encoding during data cleaning.


## 💬 How to Contribute

We welcome contributions to this project. To contribute:
1. Fork the repository.
2. Create a new branch for your feature or bug fix.
3. Commit your changes and push to your fork.
4. Submit a pull request with a description of the changes.


## 📄 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for more details.


### Acknowledgements
- **Libraries**: This project uses `pandas` for data manipulation, `chardet` for detecting file encoding, and `Tailwind CSS` for styling.
- **Chardet Documentation**: [chardet GitHub](https://github.com/chardet/chardet)
#   S t u d e n t _ B r o w s i n g _ H i s t o r y _ A n a l y s i s _ S y s t e m  
 