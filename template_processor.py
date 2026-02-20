from docxtpl import DocxTemplate
from io import BytesIO
from datetime import datetime
import os
from decimal import Decimal, ROUND_DOWN
from typing import List, Dict, Any
from utils import resource_path
import requests  # Add this import


def download_template_from_supabase(url: str):
    """
    Download template from Supabase storage and return as BytesIO.
    
    Args:
        url: Supabase URL (e.g., https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/quotations/...)
    
    Returns:
        BytesIO object containing the template
    """
    try:
        print(f"DEBUG: Downloading template from Supabase: {url}")
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        # Return the template as BytesIO
        template_bytesio = BytesIO(response.content)
        print(f"DEBUG: Successfully downloaded template ({len(response.content)} bytes)")
        
        return template_bytesio
        
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to download template from {url}: {e}")
        raise ValueError(f"Failed to download template from Supabase: {e}")
    except Exception as e:
        print(f"ERROR in download_template_from_supabase: {e}")
        raise


class QuotationTemplateProcessor:
    def __init__(self, template_source=None, template_url=None):
        """
        Initialize template processor.
        
        template_source can be:
        1. A string (local file path)
        2. A BytesIO object (file downloaded from Supabase)
        3. None if template_url is provided
        
        template_url: Supabase URL to download template from
        """
        # If template_url is provided, download from Supabase
        if template_url:
            print(f"DEBUG: Downloading template from URL: {template_url}")
            self.template_source = download_template_from_supabase(template_url)
        elif template_source is None:
            raise ValueError("Either template_source or template_url is required")
        elif isinstance(template_source, str):
            # Local file path
            self.template_source = resource_path(template_source)
            if not os.path.exists(self.template_source):
                raise FileNotFoundError(f"Template not found: {self.template_source}")
            print(f"DEBUG: Using local template: {self.template_source}")
        else:
            # Already a BytesIO object or similar
            self.template_source = template_source
            print(f"DEBUG: Using provided template source (BytesIO)")

    def process_quotation(self, quotation_data, client_data, items):
        # DocxTemplate can open both a file path and a memory stream
        doc = DocxTemplate(self.template_source)
        context = self._prepare_context(quotation_data, client_data, items)
        doc.render(context)

        output = BytesIO()
        doc.save(output)
        output.seek(0)
        return output

    def _prepare_context(self, quotation_data, client_data, items):
        """Prepare all data for template with proper empty value handling"""
        print(f"DEBUG: Starting _prepare_context")
        print(f"DEBUG: items type: {type(items)}, length: {len(items) if items else 0}")

        def safe_get(data, key, default=""):
            if isinstance(data, dict):
                value = data.get(key)
            else:
                value = getattr(data, key, None)

            if value is None or (isinstance(value, str) and value.strip() == ""):
                return default
            return value

        def format_currency(value):
            if value is None or value == 0:
                return ""
            try:
                return f"{float(value):,.2f}"
            except (ValueError, TypeError):
                return ""

        division = safe_get(quotation_data, "division")
        is_geo = division == "GEO"

        total_amount = 0.0
        items_context = []

        try:
            # ===================================================
            # GEO DIVISION → CATEGORY + SUB ITEMS
            # ===================================================
            if is_geo:
                categories = [
                    {"main_title": "Geotechnical Investigation", "subitems": []},
                    {"main_title": "Drilling & Field Works Tests", "subitems": []},
                    {"main_title": "Samples", "subitems": []},
                    {"main_title": "In-situ Testing", "subitems": []},
                    {"main_title": "Laboratory Testing", "subitems": []},
                    {"main_title": "Engineering Report", "subitems": []},
                ]

                category_keywords = [
                    ['geotechnical', 'investigation', 'test'],
                    ['drill', 'borehole', 'field', 'percussion', 'rotary'],
                    ['sample', 'specimen'],
                    ['in-situ', 'insitu', 'field test', 'penetration'],
                    ['laboratory', 'lab', 'analysis', 'chemical'],
                    ['report', 'engineering', 'document'],
                ]

                if items and isinstance(items, list):
                    for item in items:
                        description = item.get("description", "").lower()
                        unit_rate = item.get("unit_rate", 0)
                        quantity = item.get("quantity", 1)
                        amount = item.get("amount")

                        if amount is None:
                            amount = unit_rate * quantity

                        total_amount += float(amount) if amount else 0

                        category_index = 0
                        for idx, keywords in enumerate(category_keywords):
                            if any(k in description for k in keywords):
                                category_index = idx
                                break

                        categories[category_index]["subitems"].append({
                            "description": item.get("description", ""),
                            "standard": item.get("test_standard", ""),
                            "unit": item.get("unit", ""),
                            "qty": quantity,
                            "rate": unit_rate,
                            "amount": amount,
                        })

                for cat_idx, category in enumerate(categories, 1):
                    if not category["subitems"]:
                        continue

                    # Main category row
                    items_context.append({
                        "s_no": f"{cat_idx}.",
                        "description": category["main_title"],
                        "standard": "",
                        "unit": "",
                        "qty": "",
                        "rate": "",
                        "amount": "",
                    })

                    # Sub items
                    for sub_idx, sub in enumerate(category["subitems"], 1):
                        letter = chr(96 + sub_idx)
                        items_context.append({
                            "s_no": f"{cat_idx}{letter})",
                            "description": sub["description"],
                            "standard": sub["standard"],
                            "unit": sub["unit"],
                            "qty": sub["qty"],
                            "rate": format_currency(sub["rate"]),
                            "amount": format_currency(sub["amount"]),
                        })

            # ===================================================
            # NON-GEO → FLAT ITEMS ONLY (NO MAIN TITLE)
            # ===================================================
            else:
                if items and isinstance(items, list):
                    for idx, item in enumerate(items, 1):
                        unit_rate = item.get("unit_rate", 0)
                        quantity = item.get("quantity", 1)
                        amount = item.get("amount")

                        if amount is None:
                            amount = unit_rate * quantity

                        total_amount += float(amount) if amount else 0

                        items_context.append({
                            "s_no": f"{idx}.",
                            "description": item.get("description", ""),
                            "standard": item.get("test_standard", ""),
                            "unit": item.get("unit", ""),
                            "qty": quantity,
                            "rate": format_currency(unit_rate),
                            "amount": format_currency(amount),
                        })

            # ===================================================
            # TOTALS
            # ===================================================
            vat_amount = total_amount * 0.05
            grand_total = total_amount + vat_amount

            context = {
                "client_name": safe_get(client_data, "name"),
                "client_contact_person": safe_get(client_data, "contact_person"),
                "client_address": safe_get(client_data, "address"),
                "client_phone": safe_get(client_data, "phone"),
                "client_email": safe_get(client_data, "email"),

                "quotation_no": safe_get(quotation_data, "quotation_no"),
                "quotation_date": safe_get(quotation_data, "created_at", datetime.now()).strftime("%d %B, %Y"),
                "enquiry_date": safe_get(quotation_data, "enquiry_date", datetime.now()).strftime("%d %B, %Y"),
                "project_name": safe_get(quotation_data, "project_name"),
                "project_location": safe_get(quotation_data, "location"),
                "division_full_name": safe_get(quotation_data, "division_full_name", ""),

                "items": items_context,

                "total_testing_charges": format_currency(total_amount),
                "vat_amount": format_currency(vat_amount),
                "net_total": format_currency(grand_total),
                "net_total_words": self._amount_to_words(grand_total),

                "payment_terms": safe_get(
                    quotation_data,
                    "payment_terms",
                    "• 50% payment along with job confirmation.\n• 50% on draft report."
                ),
                "validity_days": safe_get(quotation_data, "validity_days", 30),
            }

            print(f"DEBUG: Context prepared successfully")
            print(f"DEBUG: Items count: {len(items_context)}")
            return context

        except Exception as e:
            print(f"ERROR in _prepare_context: {e}")
            raise

    def _amount_to_words(self, amount):
        """Convert amount to words (e.g., 250 → Two Hundred Fifty Only)"""
        def convert_to_words(num):
            """Convert a number less than 1000 to words"""
            if num == 0:
                return "Zero"
            
            ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", 
                    "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen", 
                    "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", 
                    "Nineteen"]
            tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", 
                    "Seventy", "Eighty", "Ninety"]
            
            words = ""
            
            if num >= 100:
                words += ones[num // 100] + " Hundred"
                num %= 100
                if num > 0:
                    words += " "
            
            if num >= 20:
                words += tens[num // 10]
                if num % 10 > 0:
                    words += " " + ones[num % 10]
            elif num > 0:
                words += ones[num]
            
            return words
        
        try:
            if isinstance(amount, str):
                amount = float(amount.replace(',', ''))
            
            amount = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
            
            int_part = int(amount)
            dec_part = int((amount - int_part) * 100)
            
            if int_part == 0:
                if dec_part > 0:
                    return f"{convert_to_words(dec_part)} Fils Only"
                else:
                    return "Zero Dirhams Only"
            
            words_parts = []
            
            if int_part >= 1000000:
                millions = int_part // 1000000
                words_parts.append(convert_to_words(millions) + " Million")
                int_part %= 1000000
            
            if int_part >= 1000:
                thousands = int_part // 1000
                if thousands > 0:
                    words_parts.append(convert_to_words(thousands) + " Thousand")
                int_part %= 1000
            
            if int_part > 0:
                words_parts.append(convert_to_words(int_part))
            
            words = " ".join(words_parts)
            
            result = f"{words} Dirhams"
            if dec_part > 0:
                result += f" and {convert_to_words(dec_part)} Fils"
            
            result += " Only"
            return result
            
        except (ValueError, TypeError, AttributeError) as e:
            print(f"ERROR converting amount to words: {e}, amount={amount}")
            return "Zero Dirhams Only"