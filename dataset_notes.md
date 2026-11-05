Orders span the years 2012 to 2023.
Products.Discontinued is text, with the values '0' and '1'.
Two customers have no country (NULL).
The table "Order Details" has a space and must be double-quoted.
This is a larger copy of the Northwind sample, so counts differ from the classic Northwind figures.
OrderDate is text that starts with YYYY-MM-DD and most rows also carry a time of day, so a date range must use OrderDate >= start AND OrderDate < the next day, or strftime for a year or month.
