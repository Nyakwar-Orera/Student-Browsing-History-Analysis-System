# config.py
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "fallback-key")
    DATA_FILE = os.getenv("DATA_FILE_PATH", "data/browsing_history_data_clean.csv")
    TIME_RECORDS_FILE = os.getenv("TIME_RECORDS_PATH", "data/time_records_sample.txt")
