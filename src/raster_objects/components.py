from fractions import Fraction

def label(grid,connectivity):
    h=len(grid);w=len(grid[0]);labels=[[None if v is None else 0 for v in row] for row in grid];objects=[]
    orth=[(-1,0),(0,-1),(0,1),(1,0)];steps=orth+([(-1,-1),(-1,1),(1,-1),(1,1)] if connectivity==8 else [])
    for r in range(h):
        for c in range(w):
            if grid[r][c]!=1 or labels[r][c]:continue
            oid=len(objects)+1;labels[r][c]=oid;pending=[(r,c)];cells=[];perimeter=0
            while pending:
                a,b=pending.pop();cells.append((a,b))
                for dr,dc in orth:
                    x,y=a+dr,b+dc
                    if not (0<=x<h and 0<=y<w) or grid[x][y]!=1:perimeter+=1
                for dr,dc in steps:
                    x,y=a+dr,b+dc
                    if 0<=x<h and 0<=y<w and grid[x][y]==1 and not labels[x][y]:labels[x][y]=oid;pending.append((x,y))
            objects.append({'id':oid,'cells':len(cells),'perimeter_edges':perimeter,'bbox':[min(a for a,b in cells),min(b for a,b in cells),max(a for a,b in cells),max(b for a,b in cells)],'centroid_row':str(Fraction(sum(a for a,b in cells),len(cells))),'centroid_col':str(Fraction(sum(b for a,b in cells),len(cells)))})
    return {'labels':labels,'objects':objects,'foreground_cells':sum(v==1 for row in grid for v in row),'background_cells':sum(v==0 for row in grid for v in row),'unknown_cells':sum(v is None for row in grid for v in row)}
