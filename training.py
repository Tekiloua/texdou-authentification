
def nombre_premiers(limite:int):

    def p(n:int):
        mil = (n/2)
        nb_diviseur=0
        i=2
        while i <= mil:
            if(n%i == 0):
                nb_diviseur+=1
            i+=1
        return nb_diviseur

    i=2
    while i<=limite:
        if p(i)<2 :
            yield i
        i+=1

for n in nombre_premiers(30):
    print(n)