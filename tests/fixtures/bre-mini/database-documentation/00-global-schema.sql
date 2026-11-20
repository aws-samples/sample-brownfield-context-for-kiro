-- mini schema
CREATE TABLE alpha (id INT PRIMARY KEY);
CREATE TABLE beta (id INT PRIMARY KEY, alpha_id INT REFERENCES alpha(id));
