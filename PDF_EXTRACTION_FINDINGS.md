# OAIC PDF Extraction - Correct Values

## Summary of Findings

This document contains the CORRECT values extracted from OAIC PDF files using pdfplumber text extraction.

---

## TASK 1: 2024 H1 - Average and Median Individuals Affected

**PDF**: `Notifiable-data-breaches-report-January-to-June-2024.pdf`
**Page**: 29
**Table**: Table 4: Cyber incident breakdown by average and median numbers of affected individuals worldwide

### Values Found:
- **AVERAGE (for all cyber incidents)**: 107,123 individuals
- **MEDIAN (for all cyber incidents)**: 341 individuals

### Exact Text:
```
Table 4: Cyber incident breakdown by average and median numbers of affected individuals
worldwide
                                        Average number    Median number of
                           Number of    of affected       affected
Source of breach          notifications individuals       individuals
...
Total                          201        107,123            341
```

### Breakdown by Type:
- Hacking: 14 notifications, avg: 468,713, median: 2,000
- Ransomware: 49 notifications, avg: 295,555, median: 632
- Brute-force attack: 16 notifications, avg: 21,557, median: 654
- Compromised credentials (unknown method): 49 notifications, avg: 9,376, median: 62
- Malware: 10 notifications, avg: 5,067, median: 707
- Phishing: 63 notifications, avg: 709, median: 147

---

## TASK 2: 2024 H2 - Average and Median Individuals Affected

**PDF**: `Notifiable-data-breaches-report-July-to-December-2024.pdf`
**Page**: 11
**Table**: Table 3 – Cyber incident breakdown by median and average numbers of affected individuals worldwide

### Values Found:
- **AVERAGE (for all cyber incidents)**: 15,357 individuals
- **MEDIAN (for all cyber incidents)**: 182 individuals

### Exact Text:
```
Table 3 – Cyber incident breakdown by median and average numbers of affected individuals
worldwide
                                    Number of    Median number of    Average number of
Source of breach                 notifications  affected individuals affected individuals
...
Total                                  247            182                15,357
```

### Breakdown by Type:
- Malware: 12 notifications, median: 2,229, avg: 6,358
- Ransomware: 60 notifications, median: 819, avg: 26,878
- Hacking: 23 notifications, median: 329, avg: 19,924
- Brute-force attack: 16 notifications, median: 224, avg: 21,135
- Compromised credentials (unknown method): 51 notifications, median: 89, avg: 24,672
- Phishing: 84 notifications, median: 77, avg: 1,220
- Other: 1 notification, median: 50, avg: 50

---

## TASK 3: 2020 H2 - Cyber Incidents Count

**PDF**: `notifiable-data-breaches-report-july-dec-2020.pdf`
**Page**: 8 (cyber incidents), Page 4 & 6 (total notifications)

### Value Found:
- **CYBER SECURITY INCIDENTS**: 212 notifications
- **TOTAL NOTIFICATIONS (July-Dec 2020)**: 539 notifications

### Exact Text (Page 8):
```
"Breaches attributed to cyber security incidents decreased from 218 last reporting period to 212.
This represents a decrease of 3%, roughly in line with the previous 6-monthly comparison."
```

### Exact Text (Page 4):
```
"539 breaches were notified under the scheme, an increase of 5% from the 512 notifications
received from January to June 2020."
```

### Exact Text (Page 6 - Table 1):
```
Table 1 – Notifications received in 2020 under the NDB scheme
Reporting period               Total no. of notifications
July to December 2020                    539
January to June 2020                     512
Total no. of notifications received in 2020   1,051
```

### Additional Context (Page 17):
```
"Malicious or criminal attacks remain the leading source of data breaches, accounting for 58% of
notifications. However, the number of these breaches is holding steady – down only 1% from 312
notifications last reporting period to 310.

The majority of breaches (68%) in the malicious or criminal attack category involved cyber incidents.
The OAIC received 212 notifications of cyber incidents, a slight decrease from the 218 notifications
received during the previous period. Cyber incidents were responsible for 39% of all data breaches,
with phishing, compromised or stolen credentials, and ransomware the main sources of the data
breaches in this category."
```

### Notes:
- **CYBER SECURITY INCIDENTS for July-Dec 2020**: 212
- Total notifications for July-Dec 2020: 539
- Total malicious/criminal attacks: 310 (58% of all notifications)
- Cyber incidents: 212 out of 310 malicious attacks (68% of malicious attacks)
- Cyber incidents: 212 out of 539 total notifications (39% of all notifications)
- Previous period (Jan-Jun 2020) had 218 cyber security incidents
- Represents a 3% decrease in cyber incidents

### IMPORTANT CLARIFICATION:
The task description stated "Total notifications for this period is 512" but this was incorrect:
- **512** = Total notifications for **January-June 2020** (H1 2020)
- **539** = Total notifications for **July-December 2020** (H2 2020) ← The correct period
- **212** = Cyber security incidents for July-December 2020 ← **This is the answer**

### Breakdown:
- **As percentage of all notifications**: 212/539 = 39% (as stated in PDF)
- **As percentage of malicious attacks**: 212/310 = 68% (as stated in PDF)
- **As count**: 212 notifications

---

## TASK 4: 2024 H2 - Cyber Incidents Count

**PDF**: `Notifiable-data-breaches-report-July-to-December-2024.pdf`
**Page**: 10 (Table 2) and 11 (Table 3)
**Total Notifications**: 595

### Value Found:
- **CYBER INCIDENTS**: 247 notifications

### Exact Text:
```
Table 2 – Malicious or criminal attack breakdown by median and average numbers of affected
individuals worldwide
                                    Number of
Source of breach                 notifications
Cyber incident                        247
Social engineering / impersonation    115
Rogue employee / insider threat        27
Theft of paperwork or data storage     15
Total                                 404
```

### Notes:
- This is from Table 2 which breaks down "Malicious or criminal attacks"
- Cyber incidents represent 247 out of 404 malicious attacks
- Total notifications for the period was 595
- Cyber incidents = 247/595 = 41.5% of all notifications
- Cyber incidents = 247/404 = 61.1% of malicious attacks

---

## TASK 5: 2023 H2 - Malware Notification Count (CORRECTED)

**PDF**: `Notifiable-data-breaches-report-July-to-December-2023.pdf`
**Page**: 24

### Value Found:
- **MALWARE NOTIFICATIONS**: 10 (NOT 103,569)

### Exact Text:
```
Phishing (compromised      59      1,951       70
credentials)
Malware                    10        356        9
Total                     211     56,279      171
```

### Notes:
- This is from a cyber incident breakdown table
- The table has 3 columns: Number of notifications, [likely individuals affected], [likely median]
- **CORRECT value**: 10 malware notifications
- The 103,569 in the original extraction was likely the "individuals affected" value for a different row or misread
- The actual individuals affected by malware: 356 (average) and 9 (median)

---

## TASK 6: 2022 H2 - Ransomware Notification Count (CORRECTED)

**PDF**: `OAIC-Notifiable-data-breaches-report-July-December-2022.pdf`
**Page**: 21-22

### Value Found:
- **RANSOMWARE NOTIFICATIONS**: 64 (NOT 5,064)

### Exact Text (Page 21):
```
"The top sources of cyber incidents were ransomware (29% of cyber incidents; 64 notifications),
compromised or stolen credentials (method unknown) (27%; 59 notifications) and phishing (23%; 52
notifications)."
```

### Exact Text (Page 22):
```
Chart 11 – Cyber incident breakdown
Ransomware 64 (29%)
Compromised or stolen credentials (method unknown) 59 (27%)
Phishing (compromised credentials) 52 (23%)
```

### Notes:
- **CORRECT value**: 64 ransomware notifications
- Represents 29% of cyber incidents in that period
- The 5,064 in the original extraction was likely the number of individuals affected, not notifications

---

## Summary Table

| Period    | Metric                          | Correct Value | Source           | Page |
|-----------|---------------------------------|---------------|------------------|------|
| 2024 H1   | Average individuals (cyber)     | 107,123       | Table 4          | 29   |
| 2024 H1   | Median individuals (cyber)      | 341           | Table 4          | 29   |
| 2024 H2   | Average individuals (cyber)     | 15,357        | Table 3          | 11   |
| 2024 H2   | Median individuals (cyber)      | 182           | Table 3          | 11   |
| 2024 H2   | Cyber incident count            | 247           | Table 2          | 10   |
| 2020 H2   | Cyber security incidents count  | 212           | Narrative text   | 8    |
| 2023 H2   | Malware notifications           | 10            | Table            | 24   |
| 2022 H2   | Ransomware notifications        | 64            | Chart 11         | 21-22|

---

## Notes on Terminology

The OAIC PDFs use these terms:
- **"Cyber security incidents"** or **"Cyber incidents"** - umbrella term for all cyber-related breaches
- **Malicious or criminal attacks** - includes cyber incidents, social engineering, insider threats, theft
- **Notifications** - count of breach reports (what we want)
- **Individuals affected** - count of people impacted (NOT what we want for notification counts)

The confusion in the original extraction likely came from:
1. Confusing "individuals affected" with "notifications"
2. Extracting values from the wrong column in tables
3. Text extraction artifacts causing number concatenation
