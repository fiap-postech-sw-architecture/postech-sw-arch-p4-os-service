# Achados do trivy config ignorados nos manifests (make manifests), cada um com
# o motivo.
package trivy

import rego.v1

default ignore := false

# KSV-0109 le o PASSWORD :'senha_...' do papeis.sql como senha gravada no
# ConfigMap; o valor e uma variavel do psql, lida do ambiente do container do
# banco (Secret os-postgres) pelo \getenv. So este ConfigMap: senha num outro
# continua reprovando.
ignore if {
	input.ID == "KSV-0109"
	startswith(input.Message, "ConfigMap 'os-postgres-init-")
}
