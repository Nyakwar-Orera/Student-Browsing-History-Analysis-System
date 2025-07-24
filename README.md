````markdown
# Student Browsing History Analysis System

## 📚 Browsing History Data Cleaning, Processing & Analysis

This project involves cleaning, processing, and analyzing browsing history data using Python and Flask. It provides a secure admin dashboard for viewing and interacting with detailed reports and analytics, with options to export data in CSV and PDF formats.

---

## 🚀 Deployment Instructions

### 📍 Local Setup

#### 1. 📦 Install Required Packages
Install Python dependencies:
```bash
pip install -r requirements.txt
````

#### 2. 🛠 Create a `.env` File

Create a `.env` file in the root directory and add:

```
FLASK_APP=main.py
FLASK_ENV=development
SECRET_KEY=your-secret-key-here
```

> 🔐 Replace `your-secret-key-here` with a strong secret key.

#### 3. 🧹 Run the Data Processing Script

Clean and prepare your dataset:

```bash
python process_history.py
```

#### 4. ▶️ Start the Flask Application

Run the development server:

```bash
flask run
```

#### 5. 🌐 Access the Application

Visit [http://localhost:5000](http://localhost:5000) in your browser.

---

### ☁️ Deploying to Render (Docker)

Render does not natively support Flask or PHP together, but you can deploy this Flask-based project using Docker.

#### Step 1: Add a `Dockerfile` to your project

```Dockerfile
# Use official Python image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Set environment variables
ENV FLASK_APP=main.py
ENV FLASK_RUN_HOST=0.0.0.0

# Expose port
EXPOSE 5000

# Run the app
CMD ["flask", "run"]
```

#### Step 2: Create a `render.yaml` file (optional but recommended)

```yaml
services:
  - type: web
    name: student-browsing-analysis
    env: docker
    plan: free
    region: oregon
    branch: main
    dockerfilePath: ./Dockerfile
```

#### Step 3: Push to Git and Connect to Render

1. Push your code to GitHub or GitLab.
2. Go to [Render Dashboard](https://dashboard.render.com).
3. Click **"New Web Service"** → **"Deploy from a Git repository"**.
4. Select your repo and follow the prompts.

---

## 🔑 Key Features

### ✅ Secure Admin Login

* **Password**: `admin100%SECURED`

### 📊 Dashboard (`home.html`)

* Full table of browsing history.
* Filter by **TRNO** and **Date Range**.
* Pagination, charts, CSV/PDF export.

### 📄 Student Reports (`report.html`)

* Per-student analysis: domains, usage time, logs.

### 📈 Analytics (`analytics.html`)

* Aggregate insights and behavior patterns.
* Interactive visualizations.

---

## 🧰 Data Cleaning & Processing

1. **Run `cleaningScript.py`** to fix encoding, empty rows, and column issues.
2. **Update file paths** in the script:

```python
input_path = r"C:\path\to\browsing_history_data.csv"
output_path = r"C:\path\to\browsing_history_data_cleaned.csv"
```

3. **Execute the script**:

```bash
python cleaningScript.py
```

4. **Load cleaned data**:

```python
import pandas as pd
df_cleaned = pd.read_csv("path_to_cleaned.csv")
print(df_cleaned.head())
```

---

## 💡 Tech Highlights

* **Flask** – Backend & routing
* **Tailwind CSS** – Styling and responsiveness
* **pandas** – Data processing
* **html2pdf** – PDF export
* **chardet** – Detecting file encodings
* **dotenv** – Secure config management

---

## 💬 How to Contribute

1. Fork the repo
2. Create a new branch (`feature/your-feature`)
3. Commit and push your changes
4. Open a Pull Request describing your changes

---

## 📄 License

Licensed under the MIT License. See the [LICENSE](LICENSE) file for full details.

---

## 🙏 Acknowledgements

* **pandas**, **chardet**, **Tailwind CSS**
* [chardet GitHub](https://github.com/chardet/chardet)

```
