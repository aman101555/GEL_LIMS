-- Minimal subset of the production schema (only the tables/columns the GR
-- workflow touches), used to run tests_gr_workflow.py against a SCRATCH
-- Postgres database. Never run this against production.
DROP SCHEMA public CASCADE; CREATE SCHEMA public;

CREATE TABLE users (user_id SERIAL PRIMARY KEY, username TEXT, password_hash TEXT, full_name TEXT,
                    role_id INT, created_at TIMESTAMP DEFAULT NOW(), user_role VARCHAR(20), is_active BOOLEAN DEFAULT TRUE);
CREATE TABLE quotations (quotation_id SERIAL PRIMARY KEY, quotation_no TEXT, status TEXT);
CREATE TABLE price_catalog (catalog_id SERIAL PRIMARY KEY, code TEXT, description TEXT, test_standard TEXT,
                            unit_rate NUMERIC, unit TEXT, active BOOLEAN, division_code TEXT, group_name TEXT);
CREATE TABLE quotation_items (item_id SERIAL PRIMARY KEY, quotation_id INT REFERENCES quotations(quotation_id),
                              catalog_id INT REFERENCES price_catalog(catalog_id), division_code TEXT, item_code TEXT,
                              description TEXT, test_standard TEXT, unit TEXT, unit_rate NUMERIC, quantity INT,
                              amount NUMERIC, notes TEXT, net_unit VARCHAR(255));
CREATE TABLE projects (project_id SERIAL PRIMARY KEY, project_no TEXT, quotation_id INT REFERENCES quotations(quotation_id),
                       client_id INT, project_name TEXT, location TEXT, lpo_no TEXT, lpo_date DATE, status TEXT,
                       created_at TIMESTAMP DEFAULT NOW(), client_name TEXT, lpo_verified BOOLEAN DEFAULT TRUE);
CREATE TABLE test_requests (test_request_id SERIAL PRIMARY KEY, project_id INT REFERENCES projects(project_id),
                            request_no TEXT, requested_by TEXT, status TEXT, created_at TIMESTAMP DEFAULT NOW(),
                            template_type VARCHAR(32) NOT NULL DEFAULT 'soil');
CREATE TABLE test_request_items (tri_id SERIAL PRIMARY KEY, test_request_id INT REFERENCES test_requests(test_request_id),
                                 quotation_item_id INT, quantity INT);
CREATE TABLE samples (sample_id SERIAL PRIMARY KEY, sample_no TEXT, request_id INT REFERENCES test_requests(test_request_id),
                      collected_by TEXT, received_date TIMESTAMP, status TEXT, reason_rejected TEXT, barcode TEXT,
                      storage_location TEXT, assigned_tri_id INT REFERENCES test_request_items(tri_id),
                      assigned_item_code VARCHAR(50), assigned_test_name VARCHAR(255),
                      assigned_quotation_item_id INT REFERENCES quotation_items(item_id), picture_path TEXT,
                      picture_uploaded_at TIMESTAMP, department TEXT, test_standard TEXT,
                      catalog_id INT REFERENCES price_catalog(catalog_id), batch_no TEXT, physical_sample_no TEXT,
                      unit_rate INTEGER);
CREATE TABLE reports (report_id SERIAL PRIMARY KEY, report_no VARCHAR(50), sample_id INT REFERENCES samples(sample_id),
                      worksheet_id INT, original_filename VARCHAR(255), stored_filename VARCHAR(255),
                      file_path VARCHAR(500), file_type VARCHAR(10), status VARCHAR(20), uploaded_by INT, checked_by INT,
                      approved_by INT, created_at TIMESTAMP DEFAULT NOW(), checked_at TIMESTAMP, approved_at TIMESTAMP,
                      is_locked BOOLEAN DEFAULT FALSE, covers_test_type VARCHAR(100), covers_samples TEXT[],
                      linked_to_report_id INT, notes TEXT);
