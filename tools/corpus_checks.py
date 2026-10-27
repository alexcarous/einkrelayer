"""Independent expected-content checks shared by offline and real-source review."""

import re

from bs4 import BeautifulSoup


def check_expectations(case, document):
    soup = BeautifulSoup(document.html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    errors = []
    for expected in case.get("required_text", []):
        if re.sub(r"\s+", " ", expected).strip() not in text:
            errors.append(f"Missing expected text: {expected}")
        if expected[:1].isspace() and expected not in soup.get_text():
            errors.append("Significant code whitespace changed")
    for excluded in case.get("excluded_text", []):
        if excluded in text:
            errors.append(f"Unexpected source chrome: {excluded}")
    for tag in case.get("required_tags", []):
        found = soup.select_one(".math-inline,.math-display") if tag == "math" else soup.find(tag)
        if found is None:
            errors.append(f"Missing structure: {tag}")
    order = [
        text.find(re.sub(r"\s+", " ", value).strip()) for value in case.get("required_order", [])
    ]
    if -1 in order or order != sorted(order):
        errors.append("Reading order changed")
    for image in soup.find_all("img"):
        if image.get("src") not in document.assets:
            errors.append("Missing captured image")
    for requirement in case.get("required_selectors", []):
        if not any(
            requirement["text"] in element.get_text(" ", strip=True)
            for element in soup.select(requirement["selector"])
        ):
            errors.append("Missing required structured content: " + requirement["selector"])
    for requirement in case.get("required_links", []):
        if not any(
            requirement["text"] in element.get_text(" ", strip=True)
            for element in soup.find_all("a", href=requirement["href"])
        ):
            errors.append("Missing required link: " + requirement["href"])
    return errors
