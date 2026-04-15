    lignes_csv = []

    for r in rows:

        ligne = [
            "01",                       # 1STE
            "38",                       # 2ETAB
            date_recep,                 #3 DATE RECEP
            r["code_vif"],              #4 CODE VIF FOURNISSEUR
            "01",                       #5 LIEU
            "03",                       #6 DEPOT
            r["article_code_vif"],      #7 ARTICLE
            int(r["total_kg"]),         #8 QUANTITE
            "KG",                       #9 UNITE    
            "",                         #10 LOT
            date_du_jour_plus_2,        #11 DLUO
            date_du_jour_plus_2,        #12 DLC
            "",                         #13 Lartlibel artic
            "RA"                        #14 ORIGINE
                      
        ]