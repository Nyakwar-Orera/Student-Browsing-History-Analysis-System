with open('tools/test_page.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Let's see if #printable-dossier is inside something that has display:none
# Let's replace the entire @media print with a clean, standard one:
clean_print = """
@media print {
  @page {
    size: A4 portrait;
    margin: 10mm;
  }
  nav, .container#report {
    display: none !important;
  }
  #printable-dossier {
    display: block !important;
    visibility: visible !important;
    position: static !important;
    width: 100% !important;
    height: auto !important;
    overflow: visible !important;
  }
  #printable-dossier * {
    visibility: visible !important;
  }
}
"""

# Let's replace @media print block
import re
new_content = re.sub(r'@media print\s*\{[\s\S]*?\}\s*\}', clean_print, content)

with open('tools/test_page2.html', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("Saved tools/test_page2.html")

