"""Reflow simple wide tables as labelled rows without losing column associations."""

import copy


def reflow_wide_tables(soup):
    count = 0
    for table in list(soup.find_all("table")):
        if table.find("table"):
            continue
        rows = table.find_all("tr")
        matrix = [row.find_all(["td", "th"], recursive=False) for row in rows]
        if not matrix or max(map(len, matrix), default=0) < 4:
            continue
        if any(
            cell.get("colspan", "1") != "1" or cell.get("rowspan", "1") != "1"
            for cell in table.find_all(["th", "td"])
        ):
            # Retain complex tables for layout verification rather than inventing
            # labels/relationships for merged cells.
            continue
        headers = matrix[0]
        has_headers = all(
            cell.get_text(strip=True)
            and (
                cell.name == "th"
                or all(
                    text.find_parent(["strong", "b"])
                    for text in cell.find_all(string=True)
                    if text.strip()
                )
            )
            for cell in headers
        )
        if any(len(row) != len(headers) for row in matrix):
            continue
        replacement = soup.new_tag("section", attrs={"class": "table-records"})
        if table.get("id"):
            replacement["id"] = table["id"]
        caption = table.find("caption")
        if caption:
            heading = soup.new_tag("h3")
            if caption.get("id"):
                heading["id"] = caption["id"]
            for child in caption.contents:
                heading.append(copy.copy(child))
            replacement.append(heading)
        note = soup.new_tag("p")
        note.string = "Table shown as labelled rows for narrow screens."
        replacement.append(note)
        for number, row in enumerate(matrix[1:] if has_headers else matrix, 1):
            record = soup.new_tag("section", attrs={"class": "table-record"})
            source_row = rows[number if has_headers else number - 1]
            if source_row.get("id"):
                record["id"] = source_row["id"]
            heading = soup.new_tag("h4")
            heading.string = f"Row {number}"
            record.append(heading)
            values = soup.new_tag("dl")
            for column, (header, cell) in enumerate(zip(headers, row, strict=True), 1):
                if not cell.get_text(strip=True) and not cell.find("img") and not cell.get("id"):
                    continue  # Empty cells add labels without content.
                label = soup.new_tag("dt")
                if has_headers:
                    for child in header.contents:
                        label.append(copy.copy(child))
                else:
                    label.string = f"Column {column}"
                if has_headers and header.get("id") and number == 1:
                    label["id"] = header["id"]
                if number > 1:
                    for element in label.find_all(attrs={"id": True}):
                        del element["id"]
                value = soup.new_tag("dd")
                for key in ["id", "lang", "dir", "title"]:
                    if cell.get(key):
                        value[key] = cell[key]
                for child in cell.contents:
                    value.append(copy.copy(child))
                values.append(label)
                values.append(value)
            record.append(values)
            replacement.append(record)
        table.replace_with(replacement)
        count += 1
    return count
