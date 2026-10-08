CREATE TABLE actor (
  actor_id INT NOT NULL IDENTITY ,
  first_name VARCHAR(45) NOT NULL,
  last_name VARCHAR(45) NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY NONCLUSTERED (actor_id)
  )
GO
 ALTER TABLE actor ADD CONSTRAINT [DF_actor_last_update] DEFAULT (getdate()) FOR last_update
GO
 CREATE  INDEX idx_actor_last_name ON actor(last_name) 
GO

--
-- Table structure for table country
--


CREATE TABLE country (
  country_id INT NOT NULL IDENTITY ,
  country VARCHAR(50) NOT NULL,
  last_update DATETIME,
  PRIMARY KEY NONCLUSTERED (country_id)
)
GO
 ALTER TABLE country ADD CONSTRAINT [DF_country_last_update] DEFAULT (getdate()) FOR last_update
GO

--
-- Table structure for table city
--

CREATE TABLE city (
  city_id INT NOT NULL IDENTITY ,
  city VARCHAR(50) NOT NULL,
  country_id INT NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY NONCLUSTERED (city_id),
  CONSTRAINT fk_city_country FOREIGN KEY (country_id) REFERENCES country (country_id) ON DELETE NO ACTION ON UPDATE CASCADE
)
GO
 ALTER TABLE city ADD CONSTRAINT [DF_city_last_update] DEFAULT (getdate()) FOR last_update
GO
 CREATE  INDEX idx_fk_country_id ON city(country_id) 
GO

--
-- Table structure for table address
--

CREATE TABLE address (
  address_id INT NOT NULL IDENTITY ,
  address VARCHAR(50) NOT NULL,
  address2 VARCHAR(50) DEFAULT NULL,
  district VARCHAR(20) NOT NULL,
  city_id INT  NOT NULL,
  postal_code VARCHAR(10) DEFAULT NULL,
  phone VARCHAR(20) NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY NONCLUSTERED (address_id)
