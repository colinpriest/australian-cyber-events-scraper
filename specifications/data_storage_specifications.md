# Data Storage and Modeling Specifications

This document outlines the specifications for storing, deduplicating, and modeling Australian cyber event data collected by the system.

## a) Event Deduplication Strategy

To maintain data integrity and prevent redundant entries, a robust deduplication strategy is essential. Each unique cyber event must be stored only once, even if it is reported by multiple data sources.

1.  **Unique Event Definition**: A unique cyber event is defined by its core attributes, primarily a normalized **event title** and the **approximate date of the event**.
2.  **Composite Key/Hash**: A unique identifier (e.g., a SHA-256 hash) shall be generated for each incoming event based on a normalized representation of its title and its event date (rounded to the day).
    -   Example: `hash("qantas data breach", "2025-06-30")` -> `unique_event_id`
3.  **Processing Logic**:
    -   When a new event is collected, its unique hash is calculated.
    -   The system checks if an event with this hash already exists in the `UniqueEvents` table.
    -   If it exists, the new information is treated as an update or a new source for the existing event (see sections c and e).
    -   If it does not exist, a new record is created in the `UniqueEvents` table.

## b) Extraction of Vital Attributes

Vital attributes for each cyber event must be extracted and normalized during the data preparation phase, as outlined in `specifications/cyber_data_preparation.md`. The goal is to populate a structured, canonical record for each unique event and its associated entities.

The core attributes to be extracted for a `UniqueEvent` are:

-   **Event ID**: A unique system-generated identifier (e.g., UUID).
-   **Title**: A clean, concise headline for the event (e.g., "Qantas Call Center Data Breach").
-   **Description**: A summary of the event, its nature, and its impact.
-   **Event Date**: The date the cyber incident occurred.
-   **Event Type**: A classification from a controlled taxonomy (e.g., `Ransomware`, `Data Breach`).
-   **Severity**: An assessed severity level (e.g., `Low`, `Medium`, `High`, `Critical`).
-   **Records Affected**: An integer representing the number of individuals or records impacted, if available.
-   **Status**: The current status of the event (e.g., `Active`, `Contained`, `Resolved`).

The core attributes for an `Entity` are:

-   **Entity ID**: A unique system-generated identifier.
-   **Entity Name**: The canonical name of the entity (e.g., "Qantas Airways").
-   **Entity Type**: The type of organization (e.g., `Government`, `Business`, `Not-for-profit`).
-   **Industry**: The primary industry of the entity (e.g., `Aviation`, `Healthcare`, `Finance`).
-   **Turnover**: Annual turnover, if available.
-   **Employee Count**: Number of employees, if available.

## c) Relational Table Structure

A relational database model is proposed to efficiently store the relationships between unique events, the entities they affect, and the multiple data sources that report on them.

### Table 1: `UniqueEvents`

This table holds the single, deduplicated record for each cyber incident.

| Column Name        | Data Type     | Constraints         | Description                                                  |
| ------------------ | ------------- | ------------------- | ------------------------------------------------------------ |
| `event_id`         | `UUID`        | `PRIMARY KEY`       | Unique identifier for the event.                             |
| `unique_hash`      | `VARCHAR(64)` | `UNIQUE, NOT NULL`  | The hash used for deduplication.                             |
| `title`            | `VARCHAR(255)`| `NOT NULL`          | Canonical title of the event.                                |
| `description`      | `TEXT`        |                     | A detailed summary of the event.                             |
| `event_date`       | `DATE`        | `NOT NULL`          | The date the incident occurred.                              |
| `event_type`       | `VARCHAR(50)` |                     | Type of event (e.g., 'Ransomware').                          |
| `severity`         | `VARCHAR(20)` |                     | Assessed severity (e.g., 'High').                            |
| `records_affected` | `BIGINT`      |                     | Estimated number of records or people affected.              |
| `status`           | `VARCHAR(20)` | `DEFAULT 'Active'`  | The current status of the incident.                          |
| `created_at`       | `TIMESTAMP`   | `DEFAULT NOW()`     | Timestamp of when the event was first recorded.              |
| `updated_at`       | `TIMESTAMP`   | `DEFAULT NOW()`     | Timestamp of the last update to this event record.           |

### Table 2: `Entities`

This table stores unique entities affected by cyber events. Entity attributes can be enriched over time using APIs like Perplexity.

| Column Name      | Data Type     | Constraints        | Description                                     |
| ---------------- | ------------- | ------------------ | ----------------------------------------------- |
| `entity_id`      | `INTEGER`     | `PRIMARY KEY AUTOINCREMENT` | Unique identifier for the entity.               |
| `entity_name`    | `VARCHAR(255)`| `UNIQUE, NOT NULL` | Canonical name of the entity.                   |
| `entity_type`    | `VARCHAR(50)` |                    | Type of entity (e.g., 'Business', 'Government'). |
| `industry`       | `VARCHAR(100)`|                    | Primary industry of the entity.                 |
| `turnover`       | `VARCHAR(50)` |                    | Annual turnover.                                  |
| `employee_count` | `INT`         |                    | Number of employees.                            |

### Table 3: `EventEntities` (Linking Table)

This table manages the many-to-many relationship between events and entities.

| Column Name | Data Type | Constraints                               | Description                                     |
| ----------- | --------- | ----------------------------------------- | ----------------------------------------------- |
| `event_id`  | `UUID`    | `FOREIGN KEY (UniqueEvents.event_id)`     | Links to the unique event.                      |
| `entity_id` | `INTEGER` | `FOREIGN KEY (Entities.entity_id)`        | Links to the affected entity.                   |
|             |           | `PRIMARY KEY (event_id, entity_id)`       | Ensures a unique link between event and entity. |

### Table 4: `DataSources`

This table lists the various sources from which data is collected.

| Column Name   | Data Type     | Constraints        | Description                                     |
| ------------- | ------------- | ------------------ | ----------------------------------------------- |
| `source_id`   | `INTEGER`     | `PRIMARY KEY AUTOINCREMENT` | Unique identifier for the data source.          |
| `source_name` | `VARCHAR(100)`| `UNIQUE, NOT NULL` | Name of the source (e.g., 'GDELT', 'Perplexity'). |
| `source_type` | `VARCHAR(50)` |                    | Type of source (e.g., 'API', 'Web Scrape').     |

### Table 5: `EventSources` (Linking Table)

This table links a unique event to the multiple news stories or source entries that reported it.

| Column Name      | Data Type     | Constraints                               | Description                                                  |
| ---------------- | ------------- | ----------------------------------------- | ------------------------------------------------------------ |
| `event_source_id`| `UUID`        | `PRIMARY KEY`                             | Unique identifier for this specific source entry.            |
| `event_id`       | `UUID`        | `FOREIGN KEY (UniqueEvents.event_id)`     | Links to the unique event this story is about.               |
| `source_id`      | `INTEGER`     | `FOREIGN KEY (DataSources.source_id)`     | Links to the data source that provided this story.           |
| `url`            | `VARCHAR(2048)`| `UNIQUE`                                  | The URL of the specific news article or data entry.          |
| `published_date` | `TIMESTAMP`   |                                           | The date the article was published.                          |
| `retrieved_date` | `TIMESTAMP`   | `DEFAULT NOW()`                           | The date the system collected this entry.                    |
| `raw_title`      | `TEXT`        |                                           | The original title from the source.                          |
| `raw_content`    | `TEXT`        |                                           | The snippet or raw content from the source.                  |

## d) Event Date vs. Data Source Published Dates

A clear distinction must be maintained between the date an event occurred and the date it was reported.

-   **`UniqueEvents.event_date`**: This is the canonical date of the actual cyber incident. It is the primary timestamp used for time-series analysis. The system should endeavor to find the earliest, most accurate date for the incident itself. For example, a breach may occur on January 1st but only be discovered and reported on January 15th. The `event_date` should be January 1st.

-   **`EventSources.published_date`**: This is the publication date of a specific article or report from a data source. An event that occurred on January 1st might have dozens of associated `EventSources` with `published_date` values spanning several days or weeks as the story develops. This allows for tracking the media lifecycle of an incident.

## e) Versioning of Deduplicated Event Attributes

As a cyber event unfolds, key attributes may change. For example, the initial estimate of `records_affected` may increase significantly. The data model must support the versioning of these attributes to provide a historical audit trail.

A history table is the recommended approach for this:

### Table 6: `EventAttributeHistory`

| Column Name       | Data Type      | Constraints                             | Description                                                  |
| ----------------- | -------------- | --------------------------------------- | ------------------------------------------------------------ |
| `history_id`      | `UUID`         | `PRIMARY KEY`                           | Unique identifier for the history record.                    |
| `event_id`        | `UUID`         | `FOREIGN KEY (UniqueEvents.event_id)`   | The event that was updated.                                  |
| `event_source_id` | `UUID`         | `FOREIGN KEY (EventSources.event_source_id)` | The specific source that provided the new information.       |
| `attribute_name`  | `VARCHAR(50)`  | `NOT NULL`                              | The name of the attribute that changed (e.g., 'records_affected'). |
| `old_value`       | `TEXT`         |                                         | The value of the attribute before the change.                |
| `new_value`       | `TEXT`         |                                         | The value of the attribute after the change.                 |
| `change_date`     | `TIMESTAMP`    | `DEFAULT NOW()`                         | The timestamp of when the change was recorded.               |

**Update Logic**:
1.  When processing a new `EventSource` for an existing `UniqueEvent`, the system compares its extracted attributes (e.g., `records_affected`) with the current values in the `UniqueEvents` table.
2.  If a value differs, a new record is created in `EventAttributeHistory` logging the old value, new value, and the source of the change.
3.  The `UniqueEvents` table is then updated with the new, more current value, and its `updated_at` timestamp is refreshed.