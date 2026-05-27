from database import init_db
import time

def main():
    print("Starting Suffolk Leads application...")
    init_db()
    
    # In a real application, you might start a scheduler here
    # or a web server for the dashboard API.
    while True:
        print("Application is running...")
        time.sleep(3600) # Sleep for an hour

if __name__ == "__main__":
    main()
