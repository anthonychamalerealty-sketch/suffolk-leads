from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sql_app.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class Property(Base):
    __tablename__ = "properties"
    parcel_id = Column(String, primary_key=True, index=True)
    address = Column(String)
    owner_name = Column(String)
    owner_mailing_address = Column(String)
    assessed_value = Column(Float)
    last_sale_date = Column(String)
    property_class_code = Column(String)

class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True, index=True)
    address = Column(String)
    parcel_id = Column(String, ForeignKey("properties.parcel_id"))
    source = Column(String)
    raw_data = Column(Text)
    score = Column(Float)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    status = Column(String, default="new")

class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"))
    owner_name = Column(String)
    phone = Column(String)
    email = Column(String)
    source = Column(String)

def init_db():
    Base.metadata.create_all(bind=engine)

if __name__ == "__main__":
    init_db()
