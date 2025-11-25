-- Create IndustryGroupings table for dashboard visualization
-- This table maps detailed industry values to broader categories for cleaner visualizations

CREATE TABLE IF NOT EXISTS IndustryGroupings (
    industry_group_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name TEXT NOT NULL UNIQUE,
    keywords TEXT NOT NULL,  -- JSON array of keywords to match against detailed industries
    display_order INTEGER NOT NULL,  -- Order for display in visualizations
    description TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Insert the 12 broad industry categories
-- Keywords are stored as JSON arrays for flexible matching

INSERT INTO IndustryGroupings (group_name, keywords, display_order, description) VALUES
(
    'Healthcare',
    '["Healthcare", "Medical", "Hospital", "Health", "Pharmaceutical", "Clinical", "Clinic", "Patient", "Doctor", "Nurse", "Therapy"]',
    1,
    'Healthcare providers, medical facilities, pharmaceutical companies'
),
(
    'Government & Defense',
    '["Government", "Public Sector", "Defense", "Military", "Federal", "State Government", "Local Government", "Council", "Ministry", "Department", "Agency", "Public Service"]',
    2,
    'Government agencies, defense organizations, public sector entities'
),
(
    'Financial Services',
    '["Finance", "Banking", "Financial Services", "Insurance", "Credit", "Investment", "Bank", "Fintech", "Payment", "Mortgage", "Wealth", "Asset Management"]',
    3,
    'Banks, insurance companies, investment firms, financial institutions'
),
(
    'Education',
    '["Education", "University", "School", "Academic", "College", "Student", "Learning", "Training", "Institute", "Campus"]',
    4,
    'Educational institutions, universities, schools, training providers'
),
(
    'Technology',
    '["Technology", "Software", "IT Services", "Cloud", "SaaS", "Tech", "Computing", "Data", "Analytics", "AI", "Cybersecurity"]',
    5,
    'Technology companies, software vendors, IT service providers'
),
(
    'Telecommunications',
    '["Telecommunications", "Telecom", "ISP", "Network", "Mobile", "Wireless", "Broadband", "Internet Provider", "Communications"]',
    6,
    'Telecommunications providers, ISPs, network operators'
),
(
    'Transportation',
    '["Transportation", "Logistics", "Shipping", "Aviation", "Maritime", "Airline", "Freight", "Delivery", "Courier", "Transit"]',
    7,
    'Transportation companies, logistics providers, airlines, shipping'
),
(
    'Retail',
    '["Retail", "E-commerce", "Consumer", "Shopping", "Store", "Marketplace", "Merchant", "Commerce"]',
    8,
    'Retail businesses, e-commerce platforms, consumer services'
),
(
    'Infrastructure',
    '["Energy", "Utilities", "Water", "Power", "Infrastructure", "Electricity", "Gas", "Oil", "Mining", "Construction"]',
    9,
    'Energy providers, utilities, critical infrastructure, construction'
),
(
    'Professional Services',
    '["Consulting", "Legal", "Accounting", "Professional Services", "Law Firm", "Audit", "Advisory", "Recruitment", "HR"]',
    10,
    'Consulting firms, legal services, accounting, professional advisors'
),
(
    'Not for Profit',
    '["Non-profit", "NGO", "Charity", "Foundation", "Association", "Non Profit", "Not-for-profit", "Volunteer", "Community"]',
    11,
    'Non-profit organizations, charities, NGOs, foundations'
),
(
    'Others',
    '[]',
    12,
    'Industries not matching other categories'
);

-- Create index for faster lookups
CREATE INDEX IF NOT EXISTS idx_industry_groupings_display_order
ON IndustryGroupings(display_order);

-- Create trigger to update updated_at timestamp
CREATE TRIGGER IF NOT EXISTS update_industry_groupings_timestamp
AFTER UPDATE ON IndustryGroupings
BEGIN
    UPDATE IndustryGroupings
    SET updated_at = CURRENT_TIMESTAMP
    WHERE industry_group_id = NEW.industry_group_id;
END;
