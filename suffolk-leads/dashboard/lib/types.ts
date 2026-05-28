export type LeadSource = "fire" | "probate" | "obituary" | "social";
export type LeadStatus = "new" | "contacted" | "qualified" | "disqualified" | "closed";

export interface Lead {
  id: number;
  address: string;
  source: LeadSource;
  score: number;
  created_at: string;
  status: LeadStatus;
  raw_data: string;
  parcel_id: string;
  // location tags
  state: string | null;
  county: string | null;
  // joined from contacts
  owner_name: string | null;
  phone: string | null;
  email: string | null;
  // joined from properties
  owner_mailing_address: string | null;
  assessed_value: number | null;
  last_sale_date: string | null;
  property_class_code: string | null;
}
