
from cyber_event_data import CyberEventData

def main():
    """Initializes the database and resets the review status of all events."""
    db = None
    try:
        db = CyberEventData()
        db.reset_review_status()
    finally:
        if db:
            db.close()

if __name__ == "__main__":
    main()
