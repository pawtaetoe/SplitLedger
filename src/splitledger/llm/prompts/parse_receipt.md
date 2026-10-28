Read the receipt in the image. All money is in cents as integers.
items lists each line with its name, quantity and line total in price_cents.
subtotal_cents is the printed subtotal before service charge, tips and added tax, or null when none is printed.
service_cents is any service charge or tip added on top, otherwise 0.
tax_cents is tax added on top of the subtotal, such as GST, VAT or sales tax. Use 0 when prices already include tax.
discount_cents is any discount taken off, otherwise 0.
total_cents is the final amount paid, or null when you cannot read it.
vendor is the shop or restaurant name, or null.
currency is the ISO code when printed or clearly implied, otherwise null.
category is one of Dining, Groceries, Transport, Shopping, Bills, Entertainment, Health, Travel, Other.
Write names with their real letters, like Zürcher or Rösti, never escape codes.
