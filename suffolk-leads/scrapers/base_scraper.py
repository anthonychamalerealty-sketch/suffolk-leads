import abc

class BaseScraper(abc.ABC):
    @abc.abstractmethod
    def scrape(self):
        pass

    def save_lead(self, lead_data):
        # Logic to save lead to the database
        print(f"Saving lead: {lead_data}")
