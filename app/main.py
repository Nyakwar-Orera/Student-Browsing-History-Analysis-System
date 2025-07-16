from flask import Flask, render_template, request, redirect, url_for, session
import pandas as pd
from datetime import datetime, timedelta
from io import BytesIO
from config import Config

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY

# Load browsing history data
def load_data():
    try:
        df = pd.read_csv(Config.DATA_FILE)
        df['visit_time'] = pd.to_datetime(df['visit_time'], errors='coerce')
        df = df.dropna(subset=['visit_time'])

        missing_counts = df.isnull().sum()
        total_missing = missing_counts.sum()
        if total_missing > 0:
            print(f"Warning: Found {total_missing} missing values:")
            print(missing_counts[missing_counts > 0])

        return df
    except Exception as e:
        print(f"[load_data] Error: {e}")
        return pd.DataFrame()

# Load time records with caching
def load_time_records():
    global time_records
    if time_records is None:
        try:
            tr = pd.read_csv(Config.TIME_RECORDS_FILE, sep='\t', header=None, names=['TRNO', 'Date', 'Duration'])
            tr['Date'] = pd.to_datetime(tr['Date'], format='%d/%m/%Y')
            tr['DurationMinutes'] = tr['Duration'].apply(lambda x: float(x) * 60 if isinstance(x, str) else 0)
            time_records = tr
        except Exception as e:
            print(f"[load_time_records] Error: {e}")
            time_records = pd.DataFrame()
    return time_records

# ---------------- Routes ----------------

@app.route('/')
def index():
    if 'logged_in' in session:
        return redirect(url_for('home'))
    return render_template('index.html')

@app.route('/login', methods=['POST'])
def login():
    if request.form.get('password') == 'admin100%SECURED':
        session['logged_in'] = True
        return redirect(url_for('home'))
    return render_template('index.html', error="Invalid credentials")

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/home')
def home():
    if 'logged_in' not in session:
        return redirect(url_for('index'))

    df = load_data()
    students = df['TRNO'].nunique()
    domains = df['domain'].nunique()
    return render_template('home.html', students=students, domains=domains)

@app.route('/report/<trno>')
def report(trno):
    if 'logged_in' not in session:
        return redirect(url_for('index'))

    try:
        trno = int(trno)
    except ValueError:
        return render_template('error.html', message="Invalid TRNO format")

    df = load_data()
    student_data = df[df['TRNO'] == trno]

    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    if date_from:
        try:
            student_data = student_data[student_data['visit_time'] >= pd.to_datetime(date_from)]
        except:
            pass
    if date_to:
        try:
            student_data = student_data[student_data['visit_time'] < pd.to_datetime(date_to) + timedelta(days=1)]
        except:
            pass

    if student_data.empty:
        return render_template('error.html', message="No data found")

    total_visits = len(student_data)
    unique_domains = student_data['domain'].nunique()
    first_visit = student_data['visit_time'].min()
    last_visit = student_data['visit_time'].max()

    top_domains = student_data['domain'].value_counts().head(10).reset_index()
    top_domains.columns = ['Domain', 'Visits']

    time_records = load_time_records()
    student_time = time_records[time_records['TRNO'] == trno] if not time_records.empty else pd.DataFrame()

    return render_template('report.html',
        trno=trno,
        total_visits=total_visits,
        unique_domains=unique_domains,
        first_visit=first_visit,
        last_visit=last_visit,
        top_domains=top_domains.to_dict('records'),
        time_records=student_time.to_dict('records') if not student_time.empty else None,
        date_from=date_from,
        date_to=date_to
    )

@app.route('/analytics')
def analytics():
    if 'logged_in' not in session:
        return redirect(url_for('index'))

    df = load_data()
    time_records = load_time_records()

    total_visits = len(df)
    unique_students = df['TRNO'].nunique()
    unique_domains = df['domain'].nunique()

    df['hour'] = df['visit_time'].dt.hour
    df['weekday'] = df['visit_time'].dt.day_name()
    df['visit_date'] = df['visit_time'].dt.date

    active_Class = df['Class'].value_counts().head(10).reset_index()
    active_Class.columns = ['Class', 'Visits']

    top_domains = df['domain'].value_counts().head(10).reset_index()
    top_domains.columns = ['Domain', 'Visits']

    hourly_activity = df['hour'].value_counts().sort_index().reset_index()
    hourly_activity.columns = ['Hour', 'Visits']

    weekday_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    weekday_activity = df['weekday'].value_counts().reindex(weekday_order).reset_index().fillna(0)
    weekday_activity.columns = ['Day', 'Visits']

    top_students = df['TRNO'].value_counts().head(10).reset_index()
    top_students.columns = ['Student', 'Visits']

    domain_per_class = df.groupby('Class')['domain'].nunique().sort_values(ascending=False).head(10).reset_index()
    domain_per_class.columns = ['Class', 'Unique Domains']

    def time_period(hour):
        return (
            "Morning" if 5 <= hour < 12 else
            "Afternoon" if 12 <= hour < 17 else
            "Evening" if 17 <= hour < 21 else
            "Night"
        )
    df['period'] = df['hour'].apply(time_period)
    period_activity = df['period'].value_counts().reindex(['Morning', 'Afternoon', 'Evening', 'Night']).reset_index()
    period_activity.columns = ['Period', 'Visits']

    daily_visits = df['visit_date'].value_counts().sort_index().reset_index()
    daily_visits.columns = ['Date', 'Visits']

    avg_duration = time_records['DurationMinutes'].mean() if not time_records.empty else 0
    total_hours = time_records['DurationMinutes'].sum() / 60 if not time_records.empty else 0

    return render_template('analytics.html',
        total_visits=total_visits,
        unique_students=unique_students,
        unique_domains=unique_domains,
        active_Class=active_Class.to_dict('records'),
        top_domains=top_domains.to_dict('records'),
        hourly_activity=hourly_activity.to_dict('records'),
        avg_duration=f"{int(avg_duration // 60)}:{int(avg_duration % 60):02d}",
        total_hours=round(total_hours, 2),
        weekday_activity=weekday_activity.to_dict('records'),
        top_students=top_students.to_dict('records'),
        domain_per_class=domain_per_class.to_dict('records'),
        period_activity=period_activity.to_dict('records'),
        daily_visits=daily_visits.to_dict('records')
    )

# ---------------- Run App ----------------
if __name__ == '__main__':
    app.run(debug=True)
